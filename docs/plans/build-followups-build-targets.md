# Build targets: each machine takes jobs by its own rules, and a hold stops one machine

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Each machine that can run a build job becomes a build target with its own rules. natedev runs many jobs under the limits it already has (memory gate, cargo token, build-folder turns). The Mac runs one job at a time. A future machine is added by config. A hold names one target: hana's timing runs hold natedev, while tests keep running on the Mac. `verify.sh` enforces holds itself, so no agent has to stop by hand. A job waits for a target it may use and never falls back to a held one.

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
- `ssh`: runs over ssh and takes one job at a time. The one-job lock is `mac_test.py claim`, generalized per target, and its state lives under a per-target directory.

The Mac's own settings (mirror, size budgets, CI switch, battery and disk floors) stay in `config/mac_test.conf`, named from its target entry. A future `ssh` target is a new entry plus its settings file. It reuses the same runner: probe, copy the tree, run, release.

**Holds.** Every target can be held, and a hold names its target:
- natedev's hold is the existing build hold (`~/.local/state/build-hold`, `build_hold.py`).
- The Mac's hold is the existing Mac block (`mac_test.py block`, which also turns CI's Mac switch off).
- `/build_hold hold <why> [target <name>]` defaults to natedev; `target mac` places a Mac block.
- `build_targets.py status` shows every target on one line each: free, running (what and since when), held (by whom, why, until), or unreachable.

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

#### Work Order

**Goal:** `build_targets.py status` prints one line per target (natedev, mac): free, running, held or unreachable, with who, why and since when.

**Spec:**
- `config/build_targets.conf`, in the `key=value` form `sweep.config_values` reads:
  ```
  # Machines that can run build jobs, in the order a job tries them.
  targets=mac,natedev
  kind.natedev=local
  platform.natedev=linux
  kind.mac=ssh
  platform.mac=macos
  settings.mac=mac_test.conf
  ```
- `scripts/build_targets/targets.py`:
  - `Platform = Literal["linux", "macos"]`.
  - `LocalTarget(name, platform)` and `SshTarget(name, platform, settings: Path)`, frozen dataclasses; `Target = LocalTarget | SshTarget`.
  - `read_targets(path=None) -> tuple[Target, ...]`, in the config's order. `BUILD_TARGETS_CONFIG` overrides the path, for tests. An unknown kind, a missing platform or a missing settings file is a `ConfigError` naming the key.
  - `TargetState = Free | Running(what, since) | Held(holder, reason, since, end: HoldEnd) | Unreachable(reason)`, frozen dataclasses.
  - `HoldEnd = ExpiresAt(when) | ReleaseEstimated(when) | UntilReleased`, frozen dataclasses. A Mac block lapses by itself (`ExpiresAt`); a natedev hold with an eta carries the holder's estimate (`ReleaseEstimated`); a hold with neither is `UntilReleased`. The status line prints `until <time>`, `release expected <time>`, or nothing.
  - `target_state(target) -> TargetState`. natedev is `Held` when `build_hold.py`'s holder files name a holder (reuse `read_holders`). The Mac is `Held` when `mac_test.py`'s block is active, `Running` when its claim names a live run, and otherwise `Free`. Reachability is not probed here; `status --probe` adds the Mac probe.
- `scripts/build_targets/build_targets.py status [--probe]` prints `natedev: free` / `mac: held by hana since 13:05 PDT until 15:05 PDT — <why>` / `mac: running nextest hana since 13:20 PDT`, in local time.
- Add `scripts/build_targets` to `pyrightconfig.json` the way `scripts/mac_test` is listed.

**Files:**
- `config/build_targets.conf` — new.
- `scripts/build_targets/targets.py`, `scripts/build_targets/build_targets.py` — new.
- `scripts/build_targets/test_targets.py`, `scripts/build_targets/test_build_targets.py` — new.
- `pyrightconfig.json` — one entry.

**Seats:** 1 writer + 1 tester
- `impl` — `config/build_targets.conf`, `scripts/build_targets/targets.py`, `scripts/build_targets/build_targets.py`, `pyrightconfig.json`
- `test` — `scripts/build_targets/test_targets.py` and `test_build_targets.py` from the Spec: config parsing and its errors, each state read from stand-in holder, block and claim files under temporary directories, and the status lines.

**Constraints from prior phases:** 81f216e removed the Mac's load cutoff; the probe no longer reads load.

**Acceptance gate:** `python3 -m unittest discover -s scripts/build_targets -p 'test_*.py'` green; basedpyright clean on the new files. Running `python3 scripts/build_targets/build_targets.py status` prints a line for natedev and one for the Mac.

### Phase 2 — verify.sh and app launches wait out a natedev hold by themselves  · status: todo

#### Work Order

**Goal:** While natedev is held, every local `verify.sh` step waits, naming the holder and why, and is released through the existing one-at-a-time release. The BRP launch gate refuses a launch with the same reason. No agent needs to stop by hand.

**Spec:**
- `build_targets.py wait-local --call-id <id>` returns at once with exit 0 when natedev is not held. When natedev is held, it does the following:
  - Registers the waiter in the hold's release cycle. The key is `CLAUDE_CODE_SESSION_ID` when set, or `verify:<call id>` otherwise; reuse `register_wait` and widen its key handling.
  - Prints `verify.sh: natedev is held by <holder> for <why>; waiting (tests that can run on the Mac keep running there).`
  - Polls every 15 s until its own direct release. A `verify:` waiter's release is a file written by the release driver under the cycle directory, not a socket message; `advance_release` writes it in place of `send_release` for those keys.
  - Exits 0 when released.
  - After 2 h it prints `verify.sh: natedev has been held for 2 h by <holder>; ask them.` and keeps waiting.
  - SIGINT and SIGTERM exit 130 and 143 and leave the registration to the release's existing gone-recipient handling.
- In `verify.sh`, call `wait-local` once per call, before the cargo token (926) and after the Mac attempt (899-904), so a Mac run is never delayed by a natedev hold. Record the seconds waited as `BUILDLOG_HOLD_WAIT_S`; Phase 4 adds the column.
- The memory gate's `build_hold_mark` already acknowledges admission for `CLAUDE_CODE_SESSION_ID`. Extend it to the `verify:<call id>` key, so the driver moves on after the waiter's gate returns, as it does for sessions today.
- `pre-tool-use-brp-launch-gate.sh`: when natedev is held and this session has not been released, return a PreToolUse deny with the reason `natedev is held by <holder> for <why>; launch after the release.` A released session passes through as today.
- The hold notice text is unchanged in this phase (natedev, 2026-10-11: the notice reaches hana and every showrunner, so the old notice keeps working until Phase 5 lands). `wait` and `nothing-to-build` keep working for sessions that still call them.

**Files:**
- `scripts/build_targets/build_targets.py` — `wait-local`.
- `scripts/build_hold/build_hold.py` — waiter keys, the release-file delivery, the notice text.
- `scripts/delegate/verify.sh` — the `wait-local` call and `BUILDLOG_HOLD_WAIT_S`.
- `scripts/lint/memory_gate.sh` — `build_hold_mark` for `verify:` keys.
- `scripts/hooks/pre-tool-use-brp-launch-gate.sh` — the deny.
- Tests: `scripts/build_targets/test_build_targets.py`, `scripts/build_hold/test_build_hold.py`, `scripts/delegate/test_verify_hold_wait.py` (new), `scripts/hooks/test_brp_launch_gate.py`.

**Seats:** 2 writers + 1 tester
- `impl` — `build_targets.py`, `build_hold.py`; hub: `build_hold.py` (the cycle format both sides read)
- `impl-shell` — `verify.sh`, `memory_gate.sh`, `pre-tool-use-brp-launch-gate.sh`
- `test` — the four test files from the Spec. The cases: an unheld call does not wait; a held call waits, then runs after its release; a `--filter` test passes on the Mac during a hold without waiting; the BRP deny, and its pass-through after release; the 2 h line (with an injected clock); the notice text unchanged.

**Constraints from prior phases:** Phase 1's `target_state` decides "held"; don't read holder files a second way.

**Acceptance gate:** the unittest discover lines for `scripts/build_targets`, `scripts/build_hold`, `scripts/delegate` and `scripts/hooks` green; basedpyright clean; `bash -n` on the three shell files.

### Phase 3 — A test goes to the first free target it may use, and waits instead of falling back  · status: todo

#### Work Order

**Goal:** `verify.sh test` runs on the Mac when the Mac is free, otherwise on natedev unless natedev is held. When every eligible target is held or full, it waits for the first to free up. The Mac's probe no longer turns tests away for others' processes.

**Spec:**
- `targets.py` `eligible_targets(repo_root, package, local_reason) -> tuple[Target, ...]`:
  - `macos` targets are left out when the repo has no `linux_only.<repo>` key in its settings, when the package is listed there, or when `local_reason` is set.
  - `linux` targets are always eligible.
  - Order follows `targets=`.
- `offload.py`:
  - Remove the `procs` count from `probe_script`, `parse_probe` and `ProbeSnapshot`, and the `mac_busy` decline in `claimed_run`.
  - Keep `mac_busy` in the reason list for old build-log rows.
  - Add `run --wait-s <seconds>`: when the claim is busy (11), keep trying the claim until the wait ends. `verify.sh` passes the wait only when natedev is held.
- `verify.sh` test routing, replacing the bare `try_mac_test` call at 899-904:
  1. With the Mac eligible, try it (no wait). `passed`, `failed` and `lost` are handled as today.
  2. If the Mac declined with `busy` or `blocked` and natedev is held, wait on whichever frees first: re-try the Mac claim with `--wait-s 60` in a loop, and check natedev's state between tries. When natedev's hold lifts first, continue to `wait-local` (Phase 2) and run locally.
  3. Otherwise, `wait-local`, then the token, as Phase 2 left it.
  4. Print the target in one line: `verify.sh: running on the Mac` / `verify.sh: running on natedev`.
- Record `BUILDLOG_TARGET` (`mac` | `natedev`), the target the tests ran on. For a full test that passed on the Mac and then confirmed on natedev, it is `natedev`; `BUILDLOG_MAC=passed` stays.

**Files:**
- `scripts/build_targets/targets.py` — `eligible_targets`.
- `scripts/mac_test/offload.py` — the probe and `--wait-s`.
- `scripts/delegate/verify.sh` — the routing.
- Tests: `scripts/build_targets/test_targets.py`, `scripts/mac_test/test_offload.py`, `scripts/delegate/test_verify_mac_offload.py`.

**Seats:** 2 writers + 1 tester
- `impl` — `targets.py`, `offload.py`
- `impl-shell` — `verify.sh`
- `test` — the three test files. The cases: the eligibility table; a Mac busy with another of our tests while natedev is free runs locally, as today; a Mac busy while natedev is held waits, then runs on the Mac when the claim frees; natedev's hold lifting first runs locally; a Mac blocked while natedev is held waits on natedev; a probe that sees others' cargo no longer declines.

**Constraints from prior phases:** Phase 2's `wait-local` is the only way `verify.sh` waits on natedev; Phase 1's `read_targets` order decides what is tried first.

**Acceptance gate:** the unittest discover lines for `scripts/build_targets`, `scripts/mac_test` and `scripts/delegate` green; basedpyright clean; `bash -n scripts/delegate/verify.sh`.

### Phase 4 — The build report shows tests by target and time spent waiting on holds  · status: todo

#### Work Order

**Goal:** `/builds` shows how many tests ran on each target, why tests did not reach the Mac, and the time spent waiting on a held target.

**Spec:**
- `record.py call` records `target` (from `BUILDLOG_TARGET`) and `hold_wait_s` (from `BUILDLOG_HOLD_WAIT_S`). `index.py` adds both columns and bumps `SCHEMA_VERSION` to 13. `buildlog schema` describes them.
- `report.py`:
  - "Tests on the Mac" becomes "Tests by target". It has one row per package, with columns Mac (ran, passed, failed, Mac p50), natedev (ran, p50), and "Did not reach the Mac" (busy with our test, blocked, unreachable, Linux-only or `--local`, no Mac entry).
  - Rows recorded before `target` existed read `mac` from the `mac` column as today.
- The Waiting table gains a row `Held target`: Longest, Over 5 min, Waited (calls), Total (seat-hours), Worst — from `hold_wait_s`.

**Files:**
- `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `scripts/buildlog/report.py`.
- Tests: `scripts/buildlog/test_record.py`, `test_index.py`, `test_report.py`.

**Seats:** 1 writer + 1 tester
- `impl` — the three buildlog files
- `test` — the three test files: the new columns, the schema rebuild, both report sections, and old rows without `target`.

**Constraints from prior phases:** Phase 2 exports `BUILDLOG_HOLD_WAIT_S`; Phase 3 exports `BUILDLOG_TARGET`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` green; basedpyright clean. `TZ=America/Los_Angeles ~/.claude/scripts/buildlog/buildlog report` renders both sections against the live index.

### Phase 5 — `/build_hold` takes a target, and the commands and records say how holds work now  · status: todo

#### Work Order

**Goal:** `/build_hold hold <why> [target mac]` holds the named target, natedev by default, and every command and record describes per-target holds and the tools waiting by themselves.

**Spec:**
- `build_hold.py` hold notice: the printed text says `natedev is held for <why>. verify.sh and app launches wait by themselves; tests that can run on the Mac keep running there. Stop only cargo you started outside verify.sh. No release after 2 h: ask me.`
- `build_hold.py hold --target <name>`: `natedev` is today's hold. `mac` calls `mac_test.py block --holder <session name> --for <why>` and prints its result; release with `target mac` calls `unblock`. An unknown target lists the configured ones.
- `commands/build_hold.md`:
  - Recipients do nothing for a natedev hold, because `verify.sh` and app launches wait by themselves. `build_hold.py wait` remains for a session about to run cargo outside `verify.sh`.
  - `quiet` is unchanged.
  - `target mac` places the Mac's hold.
- `commands/mac_test.md`: the block is the Mac's hold; `/build_hold hold <why> target mac` is the same thing.
- `commands/builds.md`: no change unless the report's section names appear in it.
- `docs/as-built/mac-test-offload.md`: the probe without process checks, `--wait-s`, routing.
- `docs/as-built/build-memory-admission.md`: the hold section says `verify.sh` waits by itself.

**Files:**
- `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py`.
- `commands/build_hold.md`, `commands/mac_test.md`, `commands/builds.md`.
- `docs/as-built/mac-test-offload.md`, `docs/as-built/build-memory-admission.md`.

**Seats:** 1 writer + 1 tester
- `impl` — `build_hold.py`, the commands and the docs
- `test` — `test_build_hold.py`: `--target mac` calls block and unblock through a stand-in `mac_test.py`; an unknown target fails, listing the targets; the default target is natedev.

**Constraints from prior phases:** Phases 1–4 as built.

**Acceptance gate:** `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'` green; basedpyright clean.
