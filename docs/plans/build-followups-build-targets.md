# Build targets: each machine takes jobs by its own rules, and a hold stops one machine

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Each machine that can run a build job becomes a build target with its own rules. natedev runs many jobs under the limits it already has (memory gate, cargo token, build-folder turns). The Mac runs one job at a time. A future machine gets a config entry; a second ssh machine also needs its own lock storage, which this plan does not build. A hold names one target: hana's timing runs hold natedev, while tests keep running on the Mac. `verify.sh` enforces holds itself, so no agent has to stop by hand. A job waits for a target it may use and never falls back to a held one.

> **As-built disposition: create**

> **Production: build-followups** — unit `build-report-unit`; production doc `docs/plans/build-followups-production.md`

> **Renumbered:** 2026-10-12, three insertions, all after the last `done` phase (Phase 5), so no `done` phase moved; Phases 1-5 name no later phase by number. First, "A test binary built on the Mac finds the Swift runtime" became Phase 6: the build report phase 6 → 7, the `/build_hold` target and docs phase 7 → 8. Second, "A lost Mac run keeps the Mac busy until its leftovers are gone" became Phase 7: the build report phase 7 → 8, the `/build_hold` target and docs phase 8 → 9. Third, "A silent or gone session never holds the builds behind it" became Phase 7: the lost Mac run phase 7 → 8, the build report phase 8 → 9, the `/build_hold` target and docs phase 9 → 10. Fourth, on 2026-10-12, "A build cannot point the hold check at another directory" became Phase 10: the `/build_hold` target and docs phase 10 → 11, retitled "The build commands are one group"; Phase 9 was widened and retitled in place.

## Source

2026-10-11, PDT, the user to build-report-unit directly. The first message came after a cargo-liner update said a hana build hold paused tile-fixes:

- "i saw we have a build hold but couldn't buiilds (one at a time) still be farmed to the mac?"
- "Mac's should be allowed to run a "job" even if the natedev is blocked - just like natedev can run if the mac is blocked / i feel as if we shoudl hae a more general approach to what can build where - i.e. mac is a "one at a time job" natedev is a "multiple jobs but subject to our constraints we have built into the process" - possibl other build targets could show up and we'd want to add them to the mix."
- On the Mac's load cutoff: "i would not want hana running - plus the other apps - to stop a build from running. so yes - remove that load cutoff / i'm fine with it - if i don't like it, i will ask but the default should be "go"". Landed as 81f216e, ahead of this plan.
- "and otherwise, build our more flexible model - this is approval to go"

Measured on 2026-10-11, from midnight to about 13:00 PDT: 87 of about 700 `verify.sh test` calls reached the Mac. 238 found it running another of our tests, 179 found it busy by its own checks (a load over 6, or any cargo running there), and every one of hana's holds that day stopped Mac tests along with everything else.

2026-10-12, 08:09 PDT, the user, after asking "are we rliably using the mac for builds outside of hold windows / unavailable windows?", which could not be answered without research:

- "you should not require a helper for this - we should have a series of /build: commands, backed up by scripts that give answers quickly - such as /build:report (for whatever is the most important thing to know about our builds from your research), or build:mac-report which tells about our utilization of the mac in an appropriate way - possibly showrunners should use /build:hold but i'll leave that up to you an d the showrunners to figure out. And if there are other build related commands that should group like this (with appropriate scripts) by all means, do so"

2026-10-12, 07:32 PDT, the showrunner (natedev), routing a defect to this plan: "hana reports that at 03:40 PDT a trunk Codex seat set BUILD_HOLD_DIR for its own process to get its verify.sh past a build-hold wait, after its unit's direct release while the holder file was still present. scripts/build_hold/build_hold.py line 488 reads os.environ.get("BUILD_HOLD_DIR", ...) with no check of who sets it, so any process that sets it sees an empty hold directory. Wanted: a seat (or any non-test caller) cannot point the hold check at another directory; tests keep their override."

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

**The build commands.** One group, `commands/build/`, each command backed by one script that answers in about a second: `/build:report` (the build report, opening with the few facts that matter most), `/build:mac_report` (how reliably tests reach the Mac when it is usable), `/build:hold`, `/build:mac` and `/build:targets`. The old names `/builds`, `/build_hold` and `/mac_test` are deleted with no stub, and every pointer moves in the phase that deletes the name (the showrunner, 2026-10-12). A hold directory override is honored only for a test fixture.

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

#### As-built

- `remote_test_command` in `scripts/mac_test/offload.py` sends the environment word `MACOSX_DEPLOYMENT_TARGET=14.0` ahead of the `lint` word, so it is environment and never a nextest word: `cd <mirror> && PATH="$HOME/.cargo/bin:$PATH" BUILDLOG_CALLER=verify-mac BUILDLOG_CALL_ID=<id> BUILDLOG_SYNC=1 LINT_SWEEP_BUDGET_GIB=<n> MACOSX_DEPLOYMENT_TARGET=14.0 ~/.claude/scripts/lint/lint nextest <words>; echo mac_exit=$?`.
- The value is the module constant `MACOSX_DEPLOYMENT_TARGET = "14.0"`: the version the mirrored repository's development shell exports on the Mac (the Mac's own hana checkout, `~/rust/hana`, entered by direnv from `.envrc`). It is one value for every repository, crate and settings file.
- A test binary built in the Mac's mirror is linked for that macOS version, so one that reaches a Swift bridge loads and lists its tests. Before, such a binary with no rpath aborted at load with `Library not loaded: @rpath/libswift_Concurrency.dylib`, and its run exited 104.
- Measured on 2026-10-12: the ten newest Mac runs of `hana_origami` had all exited 104. With the word, `verify.sh test hana_origami --no-cache` printed `mac_test: running on the Mac (macOS, host Mac); tree copied in 5.2 s`, ran 21 tests and passed all 21 (nextest summary 37.374 s), with no `Library not loaded` line.

**Files:**
- `scripts/mac_test/offload.py` — the constant `MACOSX_DEPLOYMENT_TARGET` and its environment word in `remote_test_command`.
- `scripts/mac_test/test_offload.py` — `test_remote_test_command_sets_development_deployment_target_before_nextest`, and the same assertions in `test_run_settings_selects_alternate_host_and_budget_over_environment_default` and in `test_probe_copy_and_run_arguments_preserve_nextest_words` (a `--filter-run` call): the word is one of the remote words and comes before the `lint` word, and the words after `lint` are exactly `nextest` and the caller's nextest words.

**Binds later work:**
- "A lost Mac run keeps the Mac busy until its leftovers are gone" edits `offload.py` and keeps the constant and its word. The tests above find the run command by its place among the recorded calls (the second ssh call, the last ssh call, the third of exactly `ssh`, `rsync`, `ssh`), so a passed run makes no call after its run command.
- "`/build_hold` takes a target, and the commands and records say how holds work now" writes the deployment target, its reason and its cost into `docs/as-built/mac-test-offload.md`.

**Gotchas:**
- The variable was unset because the remote command runs in a non-login ssh shell, and the mirror (`~/.local/state/mac-test/mirror/<repo>`) is outside `~/rust`, so direnv never runs there. With it unset, rustc links for macOS 11.0.
- The SDK's `usr/lib/swift/libswift_Concurrency.tbd` carries `$ld$previous$@rpath/libswift_Concurrency.dylib$$1$10.9$12.0$$`: a link for a macOS below 12.0 records `@rpath/libswift_Concurrency.dylib`, and a link for 12.0 or later records `/usr/lib/swift/libswift_Concurrency.dylib`, which needs no rpath. A constant below 12.0 brings the load error back.
- A crate whose own `build.rs` emits `-Wl,-rpath,/usr/lib/swift` (`hana`, `hana_clerestory`, `hana_mimesis_tools`, `hana_prosody`, `hana_video`) passed without the variable, because that argument reaches only that package's binaries. `hana_origami` reaches the bridge through its dev-dependency `hana_mimesis_tools` and has no `build.rs`, so it was the package that failed.
- The constant follows that development shell: when the shell's value changes, the constant and the tests that pin `14.0` change with it.
- A changed value costs one rebuild on the Mac. In the mirror's dep-info only `objc2` records the variable, and the build scripts of `alloca`, `clay-layout`, `objc_exception` and `ring` rerun when it changes; cargo rebuilds those crates and every crate that depends on one, and relinks their test binaries by itself. A cold Mac build of hana measured 12 min 35 s. Nothing in the mirror is deleted by hand.
- The tests read the command through the recording ssh stand-in and never contact the Mac; only a live run shows that a binary loads there. `verify.sh` calls the runner installed under `~/.claude`, so a live check of a runner in another checkout sets `VERIFY_MAC_RUNNER` to that checkout's `scripts/mac_test/offload.py` and passes `--no-cache`, because a pass already recorded for the tree returns before the runner is called.

**Ruled out:** a settings key for the value (nothing needs a second value); rustflags or an rpath argument in the remote command (the deployment target fixes every crate in one place, and the crates named above already emit that rpath for their own binaries).

### Phase 7 — A silent or gone session never holds the builds behind it  · status: done

#### As-built

- One branch of `advance_release` in `scripts/build_hold/build_hold.py` changed: the wait of an entry in `ReleasedAwaitingAdmission` or `DeliveryQueued`. Both rules below apply only to a turn with no call bound to it (an empty `active_call_id`); a turn with a bound call keeps `NO_ADMISSION_ACK_S` (900 s) and makes no lookup. Before, every turn kept that limit: measured on 2026-10-12, one release cost live sessions 15 minutes for each silent session ahead of them.
- The limit: such a turn ends as `NoAdmissionAck` after `RELEASE_SETTLE_S` (60 s) while any other entry is `AwaitingRelease`, and after `NO_ADMISSION_ACK_S` otherwise. The limit is read at every poll (`RELEASE_POLL_S`, 15 s), so the last silent entry keeps 15 minutes until another entry registers, and then ends at the next poll once its 60 s have passed. At the limit the entry ends as it did before: `active_call_id` and `session_pending` cleared, `NoAdmissionAck(started_at)` with the release or attempt instant, and the same poll sends the next entry its release message.
- The session that left: inside its limit such a turn calls `socket_for(entry["session_id"])` at each poll. On `RecipientGone` the driver clears `session_pending`, stores the state `RecipientGone()`, and the same poll serves the next entry. `RecipientLive` and `RecipientLookupFailed` leave the wait as it is and print nothing. The clock is read first, so a turn at its limit makes no lookup.
- Neither rule changes `entry["delivery"]`. A session that got its message and then left is not named in the final line; one whose message was only queued still reads `queued, no answer`; `session gone` names only a session that was gone when its turn started.
- After a cut the session is not admitted. A build it starts through `verify.sh` registers its call (`AwaitingTurn`), the entry rejoins the end of the queue as `AwaitingRelease` (`register_wait`), and its later turn opens as `ReleasedAwaitingAdmission` with no second release message (`AdmittedTurn`). Builds still start one turn at a time.
- Neither rule reaches a `verify:` key: it gets a turn only with a call, and `prune_calls` and `remove_call` move an entry whose active call is gone to `AwaitingRelease` or `NoPendingTurn`.
- This replaces two earlier rules: the shorter turn for a silent session that "verify.sh and app launches wait out a natedev hold by themselves" ruled out, and the full 15 minutes for which a told session that never answers held the release under "A release reaches every session the hold told". The 15-minute turn remains for a bound call and for a turn with no other entry `AwaitingRelease`.
- The release record's format, the final line's wording, `register_wait`, `mark_gate`, `answer_nothing_to_build`, `read_admission` and every exit code are unchanged. There is no new setting, flag, constant or type.

**Files:**
- `scripts/build_hold/build_hold.py` — the turn limit and the per-poll lookup in `advance_release`.
- `scripts/build_hold/test_build_hold.py` — 146 tests. New: `test_departed_session_ends_turn_on_next_poll_without_changing_delivery`, `test_lookup_failure_preserves_silent_turn_until_normal_deadline_without_waiters`, `test_silent_delivered_and_queued_turns_yield_at_one_minute_without_deadline_lookup`, `test_last_silent_turn_shortens_when_a_waiting_call_arrives`, `test_bound_call_keeps_full_turn_and_skips_session_lookups`, `test_call_after_silent_turn_is_cut_rejoins_queue_without_second_message`, and `test_delayed_unbound_build_yields_to_next_session_after_one_minute`, which takes the place of `test_delayed_first_build_keeps_next_session_held`. Rewritten for the new limit: `test_no_admission_ack_is_not_early`, the `timeout` ending of `test_three_told_sessions_are_saved_together_and_serve_one_turn_at_a_time`, and `test_delivered_silent_sessions_are_reached_without_final_warning`. `ReleaseStateTests.advance` passes the stand-in clock. Nine of these ten tests fail on the driver as it was before the change.

**Binds later work:** "`/build_hold` takes a target, and the commands and records say how holds work now" writes the release turn into `commands/build_hold.md` and `docs/as-built/build-memory-admission.md` (the exact condition for the one-minute limit, the session that left, the launch after a cut, cargo outside `verify.sh`), and adds the test of a launch after a cut to `scripts/build_targets/test_build_targets.py`: no test takes the session-only path through `command_check_local` after a cut. "A lost Mac run keeps the Mac busy until its leftovers are gone" and "The build report shows tests by target and time spent waiting on holds" share no file with this change and need nothing from it.

**Gotchas:**
- Another entry is `AwaitingRelease` when a build registered and waits, or when a told session has not had its turn, so told sessions that build nothing shorten each other's turns: three silent told sessions take one minute, one minute and 15 minutes.
- A launch after a cut is refused once more. `build_targets.py check-local` finds the session not admitted, prints `natedev is held by <holder> for <why>; launch after the release.` and registers the session for a later turn (`register_wait` with no call). That turn opens with no message, so the session launches again by itself.
- Cargo that a session starts outside `verify.sh` after its turn was cut is not counted; that is the cost accepted for the shorter turn.
- `RecipientGone` as a release state now also marks a session that left during its turn, with its delivery record kept.
- `mark_gate` accepts `RecipientGone` with no bound call, because a mark shows a build in flight. A session-only `WaitingForMemory` mark that arrives after its session's turn ended as `RecipientGone` reopens that turn as `WaitingForMemory`; when the marking process ends before its `MemoryGateReturned` mark, the entries not yet sent their release wait behind it for `BUILDLOG_MEM_WAIT_LIMIT_S` plus `RELEASE_SETTLE_S` (960 s by default).
- The lookup is one `sessions.py socket` process per poll, for the one entry inside a turn with no bound call, under the release lock. A lookup that keeps failing retries `LOOKUP_TRIES` (3) times, `LOOKUP_RETRY_S` (1 s) apart, so it holds the lock about 2 s in each 15 s poll.
- No live release was run. The release driver's state is shared by every session on natedev, and a changed `build_hold.py` is installed only while natedev is free and no release runs; the tests use the stand-in clock and stand-ins for `socket_for` and `send_release`.

**Ruled out:** a new constant, setting or flag for the shorter limit (`RELEASE_SETTLE_S` already holds the value); sending the release to more than one session at a time (four sessions once started builds within 73 s of one release, `docs/as-built/build-memory-admission.md`).

### Phase 8 — A lost Mac run keeps the Mac busy until its leftovers are gone  · status: done

#### As-built

- The Mac's claim ends only when the job its runner started there is known to have ended. Two things prove that: the remote command's printed `mac_exit=<n>` line, or a leftover check that counted no process. `stream_mac_run` reads a printed `mac_exit=<n>` line as a finished run whatever status ssh returned; a run with no such line is lost.
- `mac_test.py claim --pid <pid> --what <text> [--worktree <path>] [--wait <s>] [--remote <host>]`. With `--remote`, `run.json` gains `remote_host` and the claim holds after its process ends, until `release`. Without it the claim lapses with its process, as `scripts/production/mac_run.sh` claims. Types: `ClaimOwnerRecord` (`pid`, `proc_start`, `what`, `worktree`, `since`), `ProcessBoundClaim`, `RemoteJobClaim` (adds `remote_host: str`), `RunRecord = ProcessBoundClaim | RemoteJobClaim`, and `NoRemoteJob` for a claim request with no host. `read_run` returns a `RemoteJobClaim` when the record has a non-empty `remote_host`, and reads a record written before this change as process-bound.
- A kept claim is a `RemoteJobClaim` whose process is gone: `run_is_kept(run) -> TypeGuard[RemoteJobClaim]`. `run_is_live` answers true for every remote claim, so `live_run` leaves a kept record in place and a pending block waits on it.
- `claim --remote` that meets a kept claim takes it over: the record's `pid` and `proc_start` become the caller's, every other field stays, it prints `leftovers: <host>` and exits 13. This is decided before the block refusal. A second taker gets 11. `claim` without `--remote` waits as for a live run and exits 11 with `busy: leftovers of <what> on <host> since <HH:MM>`. `release --pid <pid>` removes the record whose `pid` matches, kept or live. `status` prints `running: leftovers of <what> on <host> since <time>, kept under pid <pid>`. Exits 0, 1, 10, 11 and 12 and their lines are unchanged.
- `offload.py`: `claim_arguments(request, config)` passes `--remote <config.host>`. `check_leftovers(config, host) -> LeftoversEnded | LeftoversRemain(count) | LeftoverCheckFailed(reason)` makes one ssh call under a 10 s limit, outside the controller. Its remote text (`leftover_check_script`) sends `kill`, waits one second, sends `kill -9`, counts what is left and prints `leftovers=<n>` then `rc=0`. Two selectors find a process: `pgrep -f '[.]local/state/mac-test/mirror/'` (exit 1 is "none"), and `lsof -a -u "$(id -u)" -d cwd -Fpn` read by `awk` for a working directory at or below the mirror root. Any other selector failure ends the text with no `rc=0`. `parse_leftover_check` needs `rc=0` and a decimal `leftovers`; anything else is `LeftoverCheckFailed`.
- Release: before the remote command starts, the runner releases as before. Once it started, `claimed_run` releases only after the `mac_exit` line or `LeftoversEnded`; a lost run and a signal both run the check first, and any other answer leaves the claim kept.
- Takeover in `run_offload`: `LeftoversEnded` releases, claims again and goes on (a second 13 declines `busy`); `LeftoversRemain` declines `busy`; `LeftoverCheckFailed`, an unreadable host in the reply included, writes the unreachable backoff and declines `unreachable`. No decline reason was added.
- The claim call's answer is a named value: `read_claim_answer -> FreshMacClaim | TakenOverMacClaim(host) | TakeoverHostUnreadable | MacClaimNotConfirmed(reason, stdout, stderr)`. A signal while the claim call is in flight lets it answer first (`LocalClaimAttempt`, `ProcessController.complete_claim`, `claim_for_run`): a fresh claim is released at once with no ssh call, a takeover is checked on the host its reply names.
- Lines on stderr, each silent under a matching `--last-decline`: `mac_test: <n> leftover processes of a lost run are still on the Mac; it stays busy until they end` and `mac_test: could not confirm that a lost run's leftovers ended: <reason>; the Mac stays busy`. The lost run's own line, its `lost` result, its exit 75 and an interrupted run's 128 plus the signal are unchanged.

**Files:**
- `scripts/mac_test/mac_test.py` — `--remote`, the claim types, the kept claim, the takeover, the `status` and busy lines.
- `scripts/mac_test/offload.py` — the leftover check, the release rule, the takeover, the claim answer and the deferred signal.
- `scripts/mac_test/test_mac_test.py`, `scripts/mac_test/test_offload.py` — 123 tests in the lane; the selector cases run the remote text under `sh` and `zsh -f` with stand-in `pgrep`, `lsof` and `sleep` that print no process id.

**Binds later work:** "The build commands are one group" writes the kept claim, exit 13, the two lines, the `status` line and `release --pid` into the Mac command file and `docs/as-built/mac-test-offload.md`, which still describe `pkill -f <mirror>` and a lock freed by a dead job. "`/build:report` and `/build:mac_report` answer from the build log" reads a leftover decline as `busy` and a failed check as `unreachable`; the log does not say which `busy` was leftovers until that work records the holder. `build_targets.py status` shows a kept claim as `mac: running <what> since <time>`.

**Gotchas:**
- Install `mac_test.py` and `offload.py` together, with no active Mac claim and no `mac_test.py watch` or waiting `claim` of the old code still running: the old `live_run` deletes a kept claim.
- The leftover check ends every process under the mirror root, for every repository, a person's shell or editor there included.
- The claim call runs in its own session with no time limit; it stays short because `claim` runs under `state.lock`, which never waits on a network call.
- ssh's exit status proves nothing (Tailscale SSH returns 0 whatever ran); only printed lines are read.
- No test contacts the Mac. Only a live check against the idle Mac shows that both selectors complete under macOS.

**Ruled out:** bringing the probe's process count back; a new decline reason for leftovers; reading `run.json` from `offload.py` to learn a takeover's host (the claim's reply names it).

### Phase 9 — `/build:report` and `/build:mac_report` answer from the build log in about a second  · status: todo

#### Work Order

**Goal:** `/build:report` opens with the three to five facts that matter most about the builds, shows tests by target and time waited on a held target, and `/build:mac_report` says how reliably tests reach the Mac when it is usable; each answer comes from one script in about a second, with no research step.

**Spec:**
- Work only in worktree `/home/natepiano/worktrees/claude-build-followups-build-report`, branch `build-followups-build-report`.
- Records the build log lacks today, and how older rows read. Every new writer goes through `store.append_line` (`store.py` 60) and never fails its caller (`record.py` 450).
  - `calls` gains `target` (`BUILDLOG_TARGET`) and `hold_wait_s` (`BUILDLOG_HOLD_WAIT_S`), read in `record.call` (329-353). Old rows: NULL; the target is inferred as below.
  - `calls` gains `mac_tries`: a JSON list with one entry per Mac try of the call, in order, each `{result, reason, s}`. Today only the last try is kept (`mac`, `mac_reason`, `mac_s`). Old rows: NULL, read as one try from those three columns.
  - `calls` gains `mac_holder`: for a `busy` try, the `what` of the claim that held the Mac, from the claim's `busy: <what> since <HH:MM>` line (`mac_test.py` `command_claim` 1387), a kept claim's `busy: leftovers of ...` line included. Old rows: NULL, shown as `holder not recorded`.
  - `calls` gains `after_decline`: `waited` when the call then waited on natedev's hold, `natedev` when it started there at once, empty otherwise. Old rows: NULL, read from `hold_wait_s` when present, else `not recorded`.
  - A new record kind `mac_event` and table `mac_events` (`at`, `host`, `event`, `holder`, `reason`, `until`): `blocked` and `unblocked` written by `mac_test.py` where a block record is written and removed; `unreachable` where `offload.py` writes the backoff (`write_backoff`) and `reachable` at the first probe that answers after one; `battery` and `disk` at a probe that declines for them. A stretch is the time from an opening event to its closing one. Before the first `mac_event` row there are none: the report then estimates a stretch from runs of declines with one reason and prints `estimated from declines` beside it.
  - A new record kind `hold` and table `holds` (`started_at`, `released_at`, `holder`, `reason`, `target`): written by `build_hold.py` when a hold starts and when its final release line prints. Today natedev's holds live only in the cycle files under `~/.local/state/build-hold-release`. Old days: no rows, and the report says `holds not recorded before <first row's day>`.
  - The local flag stays free text (`local_reason`). The report counts those runs apart, lists the most repeated reasons, and counts the ones whose text says the Mac was unavailable, by a fixed pattern list in the module.
  - `index.py`: `SCHEMA_VERSION` 12 → 13 (33), the new `CALL_COLUMNS` (116), the two tables in `TABLES` (243) and `ADDERS` (517); a version change rebuilds the index from the JSON lines. `buildlog schema` describes each new column in these words.
- `verify.sh` exports what `record.py` reads: `BUILDLOG_MAC_TRIES`, `BUILDLOG_MAC_HOLDER`, `BUILDLOG_AFTER_DECLINE`. It already exports `BUILDLOG_TARGET` and `BUILDLOG_HOLD_WAIT_S`.
- `scripts/buildlog/mac_report.py` (new), reached as `buildlog mac-report [--hours N]` from `cli.py` (`main` 223). One pass over the index, times in the caller's `TZ`. Default window: the last 24 hours, then one row for each of the last 3 local days.
  - A candidate is a `verify.sh test` call that reached the Mac decision for a package the Mac may run. Cache reuses, local-flag runs and repositories or packages the Mac may not run (`repo`, `linux_only`) are not candidates; each is shown as its own count.
  - Per window: candidates; finished on the Mac (passed, failed); lost mid-run; declined, by reason. Reasons seen in the data: `busy`, `mac_busy` (none since 04:38 PDT on 2026-10-12), `backoff`, `unreachable`, `battery`, `blocked`, `runner`; `disk`, `state`, `copy` and `no_tests` are printed when they occur.
  - The reliability figure: of the candidates made while the Mac was usable, the share that finished on the Mac. Set aside as not usable: `blocked`, `backoff`, `unreachable`, `battery`, `disk`, and a held target. Counted against the figure: `busy`, `mac_busy`, lost and `runner`. The line names both lists. Measured by hand for the 24 hours before 08:09 PDT on 2026-10-12: 72 of 330.
  - The largest causes of a usable Mac going unused, largest first, `busy` split by `mac_holder`.
  - Mac failures that are the environment and not the code: lost runs, `runner`, and a Mac failure whose natedev run of the same tree passed.
  - How much of each day the Mac was blocked or unreachable, from `mac_events`.
- `report.py` (shared with other units; whoever lands second resolves conflicts):
  - A lead block of three to five lines above every section, built from values the report already computes plus the new ones: the reliability figure with its counts; the longest wait of the day and its cause (memory, cargo token, build folder, held target); failed steps and flaky tests; time saved by pass records; a line for a Mac blocked or unreachable now. A line with nothing to say is left out.
  - "Tests on the Mac" (`mac_tests_section` 993, `reported_mac_calls` 893) becomes "Tests by target": one row per package, columns Mac (ran, passed, failed, Mac p50), natedev (ran, p50), and "Did not reach the Mac" (busy with our test, blocked, unreachable, state unreadable, Linux-only or `--local`, no Mac entry; `mac_busy` stays for old rows). It selects every relevant test call, a package that only ran on natedev included. Mac execution is counted from the call's Mac outcome and natedev execution from its nextest steps, so a full test that passed on the Mac and confirmed on natedev counts once on each. A cached step is no execution.
  - No count is taken from `target` alone: it names the target the call was sent to when it ended. An interrupted first Mac attempt can have an empty `mac` column and is counted in no column. An interrupted retry keeps the earlier `busy` or `blocked` decline and its seconds; it is counted under that decline, in no Mac column and no Mac time. A completed Mac pass still counts when its confirmation is interrupted.
  - `offload_record` (402) reads a declined row with no `mac_s` for `repo` and `linux_only` too.
  - The Waiting table (`waiting_section` 548, `wait_row` 448) gains `Held target`: Longest, Over 5 min, Waited (calls), Total (seat-hours), Worst.
- Commands. No stub is left: a running session picks up the new name when it restarts.
  - `commands/build/report.md`: today's `commands/builds.md` text, same script; its Owner session text schedules `/build:report`.
  - `commands/build/mac_report.md`: runs `TZ=America/Los_Angeles ~/.claude/scripts/buildlog/buildlog mac-report` and shows the output unchanged.
  - `commands/builds.md` is deleted, and every pointer moves in this phase:
    - this unit's: `commands/watcher.md` (29), `docs/as-built/build-report-session.md` (5, 16, 22, 24, 28, 29);
    - `scripts/buildlog/memory.py` (332; owner followups-unit, run done);
    - `commands/showrunner/produce.md` (460; hub, owner stalls-unit);
    - `docs/plans/build-followups-screenshot.md` (51; owner screenshot-unit);
    - `docs/plans/build-followups-production.md` (the showrunner's; not edited here, the notice asks for it).
  - Each file of another unit goes in the checkpoint notice as `also touches <path> (owner <unit>)`. Before editing one, the director checks whether another unit's branch or worktree has it in flight and tells the showrunner. `commands/showrunner/produce.md`, `commands/unit/direct.md` and `docs/production_format.md` are under edit by another unit until about 09:45 PDT on 2026-10-12: merge `build-followups` into this branch before touching one.

**Files:**
- `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `scripts/buildlog/cli.py`
- `scripts/buildlog/mac_report.py` — new.
- `scripts/buildlog/report.py`
- `scripts/buildlog/memory.py` — one pointer.
- `scripts/buildlog/test_record.py`, `scripts/buildlog/test_index.py`, `scripts/buildlog/test_report.py`, `scripts/buildlog/test_mac_report.py` — tests; the last is new.
- `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py`, `scripts/mac_test/test_mac_test.py`, `scripts/mac_test/test_offload.py` — the `mac_event` records and the holder of a `busy` try.
- `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py` — the `hold` records.
- `scripts/delegate/verify.sh`, `scripts/delegate/test_verify_mac_offload.py` — the three exports.
- `commands/build/report.md`, `commands/build/mac_report.md` — new; `commands/builds.md` — deleted.
- `commands/watcher.md`, `docs/as-built/build-report-session.md`, `commands/showrunner/produce.md`, `docs/plans/build-followups-screenshot.md` — pointers.

**Seats:** 2 writers, split by who writes a record and who reads it
- `impl` — the records: `record.py`, `index.py`, `mac_test.py`, `offload.py`, `build_hold.py`, `verify.sh` and their tests; hub: `scripts/buildlog/index.py` (the column and table names both writers use) and `scripts/buildlog/cli.py`.
- `test` — opens as `impl`: `mac_report.py`, `report.py`, `memory.py`, `test_report.py`, `test_mac_report.py`, the command files and every pointer.

**Constraints from prior phases:**
- From the unit director, 2026-10-12: until 18:05 PDT on 2026-10-12 no edit to `scripts/lint/memory_admit.py` or `scripts/buildlog/width_trial.py`, and nothing in `scripts/lint/invoke.sh` near `LINT_TEST_WIDTH`, `require_nextest` or `nextest_physical_cores`; ask the showrunner before touching lint's nextest wrapper. Python passes basedpyright with zero errors and warnings, no `Any`, no file-level ignore. A file outside this unit's list is edited only where this Work Order names it and its owner. Seats never contact the Mac and never run `verify.sh`.
- `BUILDLOG_TARGET` is `mac` (the call ended on the Mac, an interruption after the Mac was chosen included), `natedev` (tests started locally) or empty. `BUILDLOG_HOLD_WAIT_S` is a non-negative decimal of seconds waited on a hold, the seconds of each declined Mac retry included, never memory waits; test a 0.4 s wait.
- A Mac that routing excluded is `mac=declined` with `mac_reason` and `local_reason` both `repo` or `linux_only` and no `mac_s`; `--local "<why>"` keeps `mac_reason=local_flag` with the caller's words in `local_reason`.
- Since the lost-run work: a lost or interrupted run keeps the Mac's claim until a leftover check counts none. A try that meets leftovers declines `busy`; a check that cannot run declines `unreachable` and writes the backoff. `stream_mac_run` reads a printed `mac_exit=<n>` line as a finished run whatever ssh returned. `claim --remote` exits 13 on a takeover.
- `build_hold.py` changes go to the live machine only while natedev is free and no release runs; the notice says so. The hold notice text and the release record's format do not change in this phase.
- `scripts/buildlog/test_record.py` runs on temporary hold directories; keep that setup.

**Acceptance gate:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'`, `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'`, `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'` and `python3 -m unittest discover -s scripts/delegate -p 'test_verify_mac_offload.py'` green; `bash -n scripts/delegate/verify.sh`; `basedpyright scripts/buildlog scripts/mac_test scripts/build_hold` reporting `0 errors, 0 warnings` (exit 3 for the absent `.venv` is a pass). With `BUILDLOG_DIR` set to a directory the tests filled, `python3 scripts/buildlog/cli.py report` and `python3 scripts/buildlog/cli.py mac-report` each print in under 2 s, and `mac-report` prints the reliability line with both lists. `grep -rnE '(^|[^a-z_/])/builds\b|commands/builds\.md' commands scripts docs/as-built` prints nothing. Then the director's check after the merge: `mac-report` against the live index, timed.

### Phase 10 — A build cannot point the hold check at another directory  · status: todo

#### Work Order

**Goal:** A seat, or any caller that is not a test, cannot make `verify.sh` or an app launch read natedev's hold from another directory; tests keep their override.

**Spec:**
- The defect, reported by hana through the showrunner at 07:32 PDT on 2026-10-12: at 03:40 PDT a trunk Codex seat set `BUILD_HOLD_DIR` for its own process and its `verify.sh` passed a hold wait, after its unit's direct release, while the holder file was still present. `holder_directory()` (`build_hold.py` 529) returns `BUILD_HOLD_DIR` with no check of who set it, so any process that sets it sees an empty hold directory. The release directory (`BUILD_HOLD_RELEASE_DIR`, 690) is read the same way.
- Every reader and writer, from a search of `scripts`, `commands`, `docs` and `config` on 2026-10-12:
  - `scripts/build_hold/build_hold.py` 529 and 690 — the two reads.
  - `scripts/lint/memory_gate.sh` 23 — `build_hold_mark` reads `BUILD_HOLD_DIR` in shell.
  - `scripts/production/dailies_input.py` 698-709 — sets and restores `BUILD_HOLD_DIR` in its own process (owner phase-tables-unit); `scripts/production/dailies_render.py` 35 names it in a comment (owner stalls-unit).
  - 22 test files under `scripts` set one or both for a temporary directory.
  - `scripts/delegate/verify.sh` and `scripts/hooks/*.sh` name neither.
- The rule: an override is honored only for a test fixture. A directory is a test fixture when it lies under the system temporary root and holds the marker file `.build-hold-test-fixture`. Any other value is ignored: the default directory is read, and one line goes to stderr, `build hold: BUILD_HOLD_DIR ignored (<path> is no test fixture)`.
- One resolver in `build_hold.py` decides this for both directories and returns a named result (the default directory, a test fixture's directory, an ignored override with its path); no `Path | None`. `memory_gate.sh` asks the resolver or applies the same two tests in shell; it never reads the variable bare.
- Each test that sets an override writes the marker through one shared helper, so the 22 files change in one line each.
- `dailies_input.py` needs a holder directory of its own choosing in production: it passes the directory as an argument to the reader it calls and sets no variable. Name that edit in the notice as `also touches scripts/production/dailies_input.py (owner phase-tables-unit)`.
- A `verify.sh` call that ran under an honored override records `hold_dir_override` = 1 in its call row, and the build report's lead block prints the count when it is above zero, so a seat that writes the marker by hand is seen.
- Unchanged: the names `NoAdmissionAck` and `DeliveryFailed` (another unit imports them), the old hold notice text, every exit code. The checkpoint notice says whether the release record's format changed; this Work Order changes none.

**Files:**
- `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py`
- `scripts/lint/memory_gate.sh`
- `scripts/production/dailies_input.py` — owner phase-tables-unit.
- `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `scripts/buildlog/report.py` — the override count.
- `scripts/delegate/test_verify_hold_wait.py` — the marker, one line through the shared helper; the same line in each of the other test files under `scripts` that set an override (22 in all).

**Seats:** 1 writer + 1 tester
- `impl` — `build_hold.py`, `memory_gate.sh`, `dailies_input.py`, the three buildlog files
- `test` — the shared marker helper and every test file that sets an override; new cases: an override with no marker is ignored and the real hold still holds; one outside the temporary root is ignored; a fixture is honored; the stderr line; `build_hold_mark` in shell follows the same rule.

**Constraints from prior phases:**
- The director's constraints listed under the report phase apply here unchanged.
- Every fixture that can reach the hold wait needs temporary `BUILD_HOLD_DIR`, `BUILD_HOLD_RELEASE_DIR` and `PLAN_DELEGATE_ACTIVE_DIR`; a fixture that reaches the installed scripts through the real HOME rewrites the live release record.
- `build_hold_mark` runs under `set -u` with `nullglob` and stays silent on every failure.
- The report phase adds the call columns and bumps `SCHEMA_VERSION` to 13; this phase adds one column and bumps it again.
- `build_hold.py` goes to the live machine only while natedev is free and no release runs.

**Acceptance gate:** `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'`, `python3 -m unittest discover -s scripts/build_targets -p 'test_*.py'`, `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` and `python3 -m unittest discover -s scripts/production -p 'test_dailies*.py'` green; `bash -n scripts/lint/memory_gate.sh`; `basedpyright scripts/build_hold scripts/buildlog scripts/production` reporting `0 errors, 0 warnings`. The director runs the full `scripts/delegate` suite once (about 12.5 minutes).

### Phase 11 — The build commands are one group: `/build:hold`, `/build:mac`, `/build:targets`  · status: todo

#### Work Order

**Goal:** `/build:hold hold <why> [target mac]` holds the named target, natedev by default; `/build:mac` and `/build:targets` sit beside it; the old names are gone, and every command and record says how holds work now.

**Spec:**
- The group, each command backed by its existing script, beside `/build:report` and `/build:mac_report`:
  - `commands/build/hold.md` — `build_hold.py`; today's `commands/build_hold.md` rewritten as below. Showrunners use it.
  - `commands/build/mac.md` — `mac_test.py` and `audit.py`; today's `commands/mac_test.md`.
  - `commands/build/targets.md` — new: runs `python3 ~/.claude/scripts/build_targets/build_targets.py status [--probe]` and shows its lines unchanged.
  - `/verify_saved`, `/lint_config`, `/clippy` and `/validate_and_push` stay where they are: none answers a question about a build target.
- No stub is left. `commands/build_hold.md` and `commands/mac_test.md` are deleted and every pointer moves in this phase. Files that name a command, from a search on 2026-10-12 (the search pattern also matches the script directories, so confirm each line):
  - `/build_hold`: `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py`, `scripts/lint/memory_gate.sh`, `docs/as-built/build-memory-admission.md` (this unit's); `commands/showrunner/dailies.md` (owner phase-tables-unit); `docs/as-built/buildlog-tests-per-edit-rust-release.md` (owner ratio-unit, run done); `scripts/production/dailies_render.py`, `test_dailies_render.py`, `test_dailies_render_holds.py` (owner stalls-unit, retired).
  - `/mac_test`: `docs/as-built/mac-test-offload.md`, `config/README.md` (this unit's); `scripts/production/dailies_render.py`, `test_dailies_render_holds.py` (owner stalls-unit, retired).
  - `docs/plans/build-followups-production.md` is the showrunner's; the notice asks for its lines.
  - The rule for another unit's file is the report phase's: `also touches <path> (owner <unit>)` in the notice, a check for the file in flight first, and a merge of `build-followups` before `commands/showrunner/produce.md`, `commands/unit/direct.md` or `docs/production_format.md`.
- `build_hold.py` hold notice: `natedev is held for <why>. verify.sh waits by itself; an app launch is refused until the release, so launch again after it. Tests that can run on the Mac keep running there. Stop only cargo you started outside verify.sh. No release after 2 h: ask me.` The old notice text works until this phase lands.
- `build_hold.py hold --target <name>`: `natedev` is today's hold. `mac` calls `mac_test.py block --holder <session name> --for <why>` and prints its result; release with `target mac` calls `unblock`. An unknown target lists the configured ones.
- `commands/build/hold.md`:
  - Recipients do nothing for a natedev hold: `verify.sh` waits by itself, and an app launch is refused with the holder and the reason, so the recipient launches again after the release. `build_hold.py wait` remains for a session about to run cargo outside `verify.sh`. `quiet` is unchanged. `target mac` places the Mac's hold.
  - The release: a session that got its release message and stays silent gives up its turn after one minute while another session or build still waits, and the last one waiting keeps 15 minutes. The driver rechecks a turn with no call bound to it at each 15 s poll and ends it when the session is gone. A session cut that way and then launching an app is refused once more and queued; no second message comes. The final line reads `Not reached, tell them yourself: <session name> (<reason>)`, the reason one of `session gone`, `send failed`, `queued, no answer`. A session needs no `wait`; `nothing-to-build` still shortens the turn.
  - A hold directory override is honored only for a test fixture (the hold-directory phase).
- `commands/build/mac.md`: the block is the Mac's hold, and `/build:hold hold <why> target mac` is the same thing. A block stays in force after its expiry until its record is removed; name the command that clears a stuck block. A kept claim: what it is, the `status` line `running: leftovers of <what> on <host> since <time>, kept under pid <pid>`, and `release --pid <pid>` as the way out by hand. Its section on taking the Mac for a job of one's own says the lock is tied to the process only for a claim without `--remote`.
- `hold.md`, `mac.md` and `targets.md` say what to do for each state a reader can act on: `state unreadable` names the file to repair or remove; `unreachable` says to check the ssh connection (`status --probe`). `status` only reads.
- `docs/as-built/mac-test-offload.md`: the probe without process checks, `--settings`, `--last-decline`, routing, the lines a test call prints, and the deployment target (`MACOSX_DEPLOYMENT_TARGET=14.0` in `remote_test_command`, why, and that a changed value costs one rebuild). Correct line 13 (`run.json` gains `remote_host`), 14 (`--remote`, exit 13), 28-30 (usage, the decline line, no process count), 33, 75 and 87 (the kept claim, the leftover check and its two selectors, the takeover, the two stderr lines), 40 (an interrupted Mac call writes a call record), 53 and 55 (the schema and the report as the report phase leaves them), 68 (a job that dies frees the lock only without `--remote`). Add: a printed `mac_exit=<n>` line is a finished run whatever ssh returned; a signal while the claim call is in flight lets it answer first.
- `docs/as-built/build-memory-admission.md`: `verify.sh` waits by itself and an app launch is refused until the release; a mark needs no session id (53); the launch hook does not always go on to memory admission (61); the hold recheck after each memory admission; hold time recorded apart from memory waits; the release record grouped by session with its holder generation; the delivery outcomes and the not-reached clause; the promote rule. At 162-168: `NoPendingTurn` joins the release states; `socket_for` returns `RecipientLookupFailed`; a told session is released registered or not; a turn with no bound call ends after `RELEASE_SETTLE_S` while another entry is `AwaitingRelease`, and as `RecipientGone` when the session left; a bound call keeps `NO_ADMISSION_ACK_S`.
- `write_hold` and `start_hold` in `build_hold.py` take `label: str | None = None`; the notice change passes through both, so replace that parameter with one resolved holder identity (its key and the name to show). Callers outside `scripts/build_hold`: `scripts/build_targets/test_build_targets.py` (two calls) and `scripts/delegate/test_verify_mac_offload.py` (two calls), each with five positional arguments and no label.

**Files:**
- `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py`
- `scripts/build_targets/test_build_targets.py` — the launch after a cut turn; its two `start_hold` calls.
- `scripts/delegate/test_verify_mac_offload.py` — its two `start_hold` calls.
- `commands/build/hold.md`, `commands/build/mac.md`, `commands/build/targets.md` — new; `commands/build_hold.md`, `commands/mac_test.md` — deleted.
- `scripts/lint/memory_gate.sh`, `config/README.md`, `commands/showrunner/dailies.md`, `scripts/production/dailies_render.py`, `scripts/production/test_dailies_render.py`, `scripts/production/test_dailies_render_holds.py`, `docs/as-built/buildlog-tests-per-edit-rust-release.md` — pointers.
- `docs/as-built/mac-test-offload.md`, `docs/as-built/build-memory-admission.md`

**Seats:** 1 writer + 1 tester
- `impl` — `build_hold.py`, the command files, the pointers outside tests, the two as-built docs
- `test` — `test_build_hold.py` (`--target mac` calls block and unblock through a stand-in `mac_test.py`; an unknown target fails, listing the targets; the default is natedev), `test_build_targets.py` (after an unbound turn is cut, `check-local` returns 10 with the refusal and requeues the session without a call; its later turn sends no second message, and another `check-local` returns 0 while the holder remains), `test_verify_mac_offload.py` and the two `test_dailies_render*.py` files.

**Constraints from prior phases:**
- The director's constraints listed under the report phase apply here unchanged.
- `build_targets.py status` prints five states: free, running, held, unreachable, state unreadable; `--probe` adds a read-only ssh check. It shows a kept claim as `mac: running <what> since <time>`.
- A build's waiter is keyed by the session that owns its delegate run, then `CLAUDE_CODE_SESSION_ID`, then `verify:<call id>`. An unreadable hold state stops a build with exit 12; a test whose route can still end on the Mac goes on to it.
- What a test call prints: on the Mac, the runner's `mac_test: running on the Mac (macOS, host <host>); tree copied in <N> s`; on natedev, `verify.sh: running on natedev`; a decline, `mac_test: not running on the Mac (<reason>)`; while it waits on both, `verify.sh: natedev is held by <holder> for <reason>.` and `verify.sh: waiting for the Mac (<reason>) or natedev's release.`, each printed once and again only on a change.
- After a cut turn, `check-local` prints `natedev is held by <holder> for <why>; launch after the release.` and registers the session with no call; that turn opens as `ReleasedAwaitingAdmission` with no message. No test takes that path through `command_check_local` yet; `WaitLocalTests` in `test_build_targets.py` has the fixtures (`begin_hold`, `release_turn`, stand-ins for `socket_for` and `send_release`).
- The lost-run work: `mac_test.py` has `RunRecord = ProcessBoundClaim | RemoteJobClaim` and `run_is_kept`; `offload.py` has `check_leftovers`, `LeftoversEnded | LeftoversRemain | LeftoverCheckFailed`, and the claim answers `FreshMacClaim | TakenOverMacClaim | TakeoverHostUnreadable | MacClaimNotConfirmed`.
- The report's sections are the lead block, "Tests by target" and the Waiting table's `Held target` row; the Mac report is `buildlog mac-report`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'`, `python3 -m unittest discover -s scripts/build_targets -p 'test_*.py'`, `python3 -m unittest discover -s scripts/production -p 'test_dailies_render*.py'` and `python3 -m unittest discover -s scripts/delegate -p 'test_verify_mac_offload.py'` green; `basedpyright scripts/build_hold scripts/build_targets scripts/delegate scripts/production` reporting `0 errors, 0 warnings` (exit 3 for the absent `.venv` is a pass). `grep -rnE '(^|[^a-z_/])/build_hold\b|(^|[^a-z_/.~])/mac_test\b|commands/(build_hold|mac_test)\.md' commands scripts docs/as-built config` prints nothing.
