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

#### Work Order

**Goal:** While natedev is held, every local `verify.sh` step waits, naming the holder and why, and is released through the existing one-at-a-time release. The BRP launch gate refuses a launch with the same reason. No agent needs to stop by hand.

**Spec:**
- `build_targets.py wait-local --call-id <id>` returns at once with exit 0 when natedev is not held. When natedev is held, it does the following:
  - Registers the waiter in the hold's release cycle under its waiter key; reuse `register_wait` and widen its key handling. The key comes from `build_hold.waiter_key(call_id)`, new in this phase: the session Phase 2's `owning_session()` resolves, else `verify:<call id>`. The owner key groups its callers in one entry: the entry lists each pending call id with the call's pid and process-start identity, and holds one active admission turn at a time. A release authorizes the entry's active call; the gate's marks are bound to that call and cycle; when its turn ends, the next pending call of the same key rejoins the queue while a hold remains. A call that registers after its key's entry reached `NothingToBuild`, `MemoryGateReturned`, `NoAdmissionAck`, `NoRegistration`, `RecipientGone` or `DeliveryFailed` rejoins the queue the same way.
  - Prints `verify.sh: natedev is held by <holder> for <why>; waiting (tests that can run on the Mac keep running there).`
  - Polls its entry in the cycle record every 15 s. The record authorizes admission for every key; a socket message only notifies a Claude session, and a `verify:` key gets none (`advance_release` marks it released without `send_release`). Per state: `AwaitingRelease` waits; `DeliveryQueued` and `ReleasedAwaitingAdmission` authorize the active call; `WaitingForMemory` belongs to that already-authorized call; `MemoryGateReturned` completes its admission.
  - The admission read returns typed results, never strings or `HoldCycle | None`: `NoCycle` (wait on the hold itself until it clears), `AwaitingTurn`, `AdmittedTurn`, and a read-error case that prints its diagnostic and exits 12. A changed cycle id means register again.
  - Exits 0 when released.
  - After 2 h it prints `verify.sh: natedev has been held for 2 h by <holder>; ask them.` and keeps waiting.
  - SIGINT and SIGTERM remove this call from its entry and exit 130 and 143. The driver checks each pending call's pid and process-start identity before granting or awaiting its admission and drops a call whose process is gone or whose pid was reused, without touching other calls under the same key.
- In `verify.sh`, call `wait-local` once per call, before the cargo token (926) and after the Mac attempt (899-904), so a Mac run is never delayed by a natedev hold. A hold can begin after that first wait, so `scripts/lint/invoke.sh` revalidates admission against the current hold cycle before each local compiling step, beside its `build_hold_mark` call. Record the seconds waited as `BUILDLOG_HOLD_WAIT_S`; Phase 5 adds the column.
- Admission lasts for the call, within one holder generation. A call admitted at `wait-local` or at a step keeps its admission for its remaining steps while the set of holders is unchanged, so it never requeues behind another call while it holds its run's cargo token. A holder that joins, also one joining the same cycle id (`register_wait` 604), ends that for steps not yet started: the call waits again before its next compiling step, and on release a call resuming mid-call is admitted ahead of every call that has not started a step, in its own key and across keys, because it already holds the cargo token and its build-folder turn. The cycle record carries the holder generation; its format is the writer's.
- The per-step check in `invoke.sh` runs only for a local `verify.sh` call: it has a call id and is not the Mac's run (`BUILDLOG_CALLER=verify-mac`). The standalone `scripts/lint/lint` caller and the Mac's run keep today's memory-gate behavior and never register a waiter.
- `run_once` runs in a pipeline subshell (`verify.sh` 157), so a variable set there never reaches the parent. Each wait appends its seconds to a per-call temporary file; the parent sums it into `BUILDLOG_HOLD_WAIT_S` before it records completion or interruption, then removes the file.
- A failed notification never cancels a live call's demand: when the driver records `RecipientGone` or `DeliveryFailed` for a key whose entry lists a live pending call, the record still grants that call its turn.
- A session-level registration (`build_hold.py wait`, the BRP gate) and a mark without `--call-id` use the key's session turn. They never consume or complete the turn of an active `verify.sh` call under the same key.
- The memory gate's `build_hold_mark` passes the call id (`mark --call-id <id>`), and `build_hold.py mark` marks the key `waiter_key` resolves and binds the mark to that call, so the driver moves on after the waiter's gate returns, for a session's own call, a seat's call and a `verify:` call alike.
- `pre-tool-use-brp-launch-gate.sh`: when natedev is held and this session has not been released, return a PreToolUse deny with the reason `natedev is held by <holder> for <why>; launch after the release.` A released session passes through as today. The hook uses the same admission decision as `wait-local`: it denies `StateUnreadable` with its diagnostic, and does no memory admission and no mark when it denies.
- The hold notice text is unchanged in this phase (natedev, 2026-10-11: the notice reaches hana and every showrunner, so the old notice keeps working until Phase 6 lands). `wait` and `nothing-to-build` keep working for sessions that still call them.

**Files:**
- `scripts/build_targets/build_targets.py` — `wait-local`.
- `scripts/build_hold/build_hold.py` — `waiter_key`, grouped entries with call ids, the typed admission read, the driver's abandoned-call check.
- `scripts/delegate/verify.sh` — the `wait-local` call and `BUILDLOG_HOLD_WAIT_S`.
- `scripts/lint/memory_gate.sh` — `build_hold_mark` passes the call id.
- `scripts/lint/invoke.sh` — the admission check before each local compiling step.
- `scripts/hooks/pre-tool-use-brp-launch-gate.sh` — the deny.
- `scripts/build_targets/test_build_targets.py` — tests.
- `scripts/build_hold/test_build_hold.py` — tests.
- `scripts/delegate/test_verify_hold_wait.py` — tests, a new file.
- `scripts/delegate/test_verify_release_ack.py` — tests.
- `scripts/hooks/test_brp_launch_gate.py` — tests.

**Seats:** 1 writer + 1 tester
- `impl` — `build_targets.py`, `build_hold.py`, `verify.sh`, `memory_gate.sh`, `invoke.sh`, `pre-tool-use-brp-launch-gate.sh`; sole owner of the cycle format
- `test` — the five test files from the Files list, and the `VerifyRoutingTests` fixture correction named under Constraints. The cases: two calls under one owner key at once; a seat's call after its owner answered `nothing-to-build`; a call arriving after its key's turn finished; every entry state, queued delivery included; SIGINT, SIGTERM, a vanished process and a reused pid; a missing, changed and unreadable cycle record; a hold that begins between two compiling steps; the BRP deny for unreadable state; an unheld call does not wait; a held call waits, then runs after its release; a `--filter` test passes on the Mac during a hold without waiting; the BRP deny, and its pass-through after release; the 2 h line (with an injected clock); the notice text unchanged; a call from a delegate run whose shell carries another session's `CLAUDE_CODE_SESSION_ID` registers and marks under the run's owning session; a call with no run and no session id uses `verify:<call id>`; unreadable hold state exits 12 without waiting; an admitted call does not requeue behind a peer in the same holder generation; a holder joining mid-call makes the call wait at its next step, and it is admitted before a pending call that has not started; an already-waiting call whose notification ends `RecipientGone` or `DeliveryFailed` still gets its turn; a BRP launch while a seat's call under the same owner holds the active turn; neither the Mac's run nor a standalone lint caller registers a waiter; the recorded `BUILDLOG_HOLD_WAIT_S` includes a wait that began between two steps.

**Constraints from prior phases:**
- Phase 1's `target_state` decides "held"; don't read holder files a second way.
- Phase 2's `owning_session()` is the only place that decides which session owns a call; `waiter_key` wraps it and adds nothing but the `verify:<call id>` fallback.
- Phase 2 left `build_hold.py mark` resolving the owner when no `--session-id` is given (`OwningSession(session_id) | NoOwningSession`; `NoOwningSession` prints `NoSessionId` and writes nothing), and `build_hold_mark` in `memory_gate.sh` passing no id and returning 0 when the holder directory holds no file. Keep both for a mark without `--call-id`. `test_call_without_session_or_run_writes_no_admission_mark_through_verify` in `scripts/delegate/test_verify_release_ack.py` expects an untouched record for an ownerless call during a hold; from this phase that call registers under `verify:<call id>`, so the test asserts the registration and the admission after release, and keeps the untouched-record assertion for a call that performs no admission.
- In `scripts/delegate/test_verify_release_ack.py`, `VerifyRoutingTests` reads canned cargo metadata through a stand-in (`write_command_stand_ins`, the `WorkspaceTarget`, `WorkspacePackage` and `WorkspaceMetadata` TypedDicts, a second `stubs/cargo`). Put the shared fixture's real metadata path back: restore `REAL_CARGO` (from `rustup which cargo`) and `RUSTUP_HOME` in `setUp`, remove the cargo stand-in and the three TypedDicts, and keep only the isolation the cases need (`PLAN_DELEGATE_ACTIVE_DIR`, `PLAN_DELEGATE_BOARD_DIR`, `PLAN_DELEGATE_TEAM_ROLE`, `NOTIFIER_SESSIONS_DIR`, `MAC_TEST_STATE_DIR`, `VERIFY_MAC_RUNNER`). These tests read `cargo metadata` and stub every other cargo call, so they start no build and run during a hold.
- `TargetState = Free | Running | Held | Unreachable | StateUnreadable` (`scripts/build_targets/targets.py`). For natedev, `target_state` returns `Held` for any holder file, also one whose content cannot be decoded (`build_hold.read_holder` keeps such a file an active hold), and `StateUnreadable(reason)` when the holder directory or a holder file cannot be accessed. `Unreachable` comes only from `status --probe`.
- `wait-local` on `StateUnreadable`: print `verify.sh: natedev's hold state cannot be read (<reason>); not starting.` and exit 12, the code `mac_test.py claim` uses for unreadable state. A build never starts on a hold state nobody can read.
- `HoldEnd = ExpiresAt | ReleaseEstimated | UntilReleased | AwaitingBlockRemoval`; a natedev hold carries `ReleaseEstimated` or `UntilReleased`.
- `build_targets.display_field` collapses whitespace in one displayed field (`format_state` renders a whole status line with it); use `display_field(state.holder)` and `display_field(state.reason)` in the waiting line and the deny reason.
- From the showrunner, 2026-10-11, required while another unit's test-width trial runs: make no edit to `scripts/lint/memory_admit.py` or `scripts/buildlog/width_trial.py`.
- In `scripts/lint/invoke.sh` the hold wait ends before the step's start clock (`BUILDLOG_START`) is taken for the record, is never added to `BUILDLOG_MEM_WAIT_S`, and is recorded only in `BUILDLOG_HOLD_WAIT_S`.
- For the nextest step the hold wait happens before `run_nextest` picks its width: the wait sits at the top of `run_nextest`, after `require_nextest` and before the width choice. Change nothing on the `LINT_TEST_WIDTH` line, in `require_nextest` or in `nextest_physical_cores`.

**Acceptance gate:** the unittest discover lines for `scripts/build_targets`, `scripts/build_hold`, `scripts/delegate` and `scripts/hooks` green; basedpyright clean; `bash -n` on the four shell files.

### Phase 4 — A test goes to the first free target it may use, and waits instead of falling back  · status: todo

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
- `verify.sh` test routing, replacing the bare `try_mac_test` call at 899-904:
  1. Visit the eligible targets in the order printed. A natedev that comes first and is not held runs locally at once, with no Mac attempt; a test with `targets=natedev,mac` asserts `offload.py` is never called then. Otherwise try the Mac (no wait). `passed`, `failed` and `lost` are handled as today.
  2. If the Mac declined with `busy` or `blocked` and natedev is held, wait on whichever frees first. Register local demand through Phase 3's waiter and poll its admission decision between Mac tries; never watch the holder file, which stays in place while a release admits callers one at a time. Re-try the Mac claim without blocking, paced 15 s apart. When local admission comes first, run locally; when a `--filter` run finishes on the Mac, withdraw the pending local demand.
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

**Acceptance gate:** the unittest discover lines for `scripts/build_targets`, `scripts/mac_test` and `scripts/delegate` green; basedpyright clean; `bash -n scripts/delegate/verify.sh`.

### Phase 5 — The build report shows tests by target and time spent waiting on holds  · status: todo

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

**Constraints from prior phases:** Phase 3 exports `BUILDLOG_HOLD_WAIT_S` and Phase 4 adds the routing loop's waits to it; Phase 4 exports `BUILDLOG_TARGET`, which names where the tests last ran and cannot by itself say that a call also ran on the Mac.

**Acceptance gate:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` green; basedpyright clean. `python3 scripts/buildlog/cli.py report`, run from the worktree root with `BUILDLOG_DIR` set to a temporary directory the tests populated, renders both sections. The installed `~/.claude/scripts/buildlog/buildlog` runs another checkout's code, so it is no gate for this phase; rendering against the live index is a separate check after the merge.

### Phase 6 — `/build_hold` takes a target, and the commands and records say how holds work now  · status: todo

#### Work Order

**Goal:** `/build_hold hold <why> [target mac]` holds the named target, natedev by default, and every command and record describes per-target holds and the tools waiting by themselves.

**Spec:**
- `build_hold.py` hold notice: the printed text says `natedev is held for <why>. verify.sh and app launches wait by themselves; tests that can run on the Mac keep running there. Stop only cargo you started outside verify.sh. No release after 2 h: ask me.`
- `build_hold.py hold --target <name>`: `natedev` is today's hold. `mac` calls `mac_test.py block --holder <session name> --for <why>` and prints its result; release with `target mac` calls `unblock`. An unknown target lists the configured ones.
- `commands/build_hold.md`:
  - Recipients do nothing for a natedev hold, because `verify.sh` and app launches wait by themselves. `build_hold.py wait` remains for a session about to run cargo outside `verify.sh`.
  - `quiet` is unchanged.
  - `target mac` places the Mac's hold.
- `commands/mac_test.md`: the block is the Mac's hold; `/build_hold hold <why> target mac` is the same thing. A block stays in force after its expiry time until its record is removed; name the existing command that clears a stuck block.
- `commands/build_hold.md` and `commands/mac_test.md` both point to `build_targets.py status` as the one place to read every target's state, and say what to do for each state a reader can act on: `state unreadable` names the file to repair or remove; `unreachable` says to check the ssh connection (`status --probe`). `status` only reads: it never settles a block or changes a record.
- `commands/builds.md`: no change unless the report's section names appear in it.
- `docs/as-built/mac-test-offload.md`: the probe without process checks, `--settings`, routing.
- `docs/as-built/build-memory-admission.md`: the hold section says `verify.sh` waits by itself.

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
- Admission is automatic: `verify.sh` and the BRP launch gate wait or refuse by themselves, and an unreadable hold state stops a build with exit 12 (Phase 3). Routing sends a test to natedev when the Mac's state cannot be read (Phase 4).
- The report's sections are "Tests by target" and the Waiting table's `Held target` row (Phase 5).

**Acceptance gate:** `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'` green; basedpyright clean.
