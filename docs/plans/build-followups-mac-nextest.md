# Mac opportunistic nextest

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** `verify.sh test` tries the Mac first when it is free, and a showrunner can block the Mac and is told when it is free.

> **As-built disposition: create**

> **Production: build-followups** — unit `build-report-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` config repo: scripts and commands every Claude Code and Codex session on natedev and the Mac runs. This plan sends `verify.sh test` runs to the Mac when it is idle.
- **Project started:** 2026-10-07T18:50:28.798+00:00
- **Stack:** Python 3 standard library only (`unittest`, `argparse`, `fcntl`, `subprocess`, `shlex`, `tomllib`), checked by basedpyright; bash (`scripts/delegate/verify.sh`, `scripts/lint/invoke.sh`); zsh (`scripts/production/mac_run.sh`); `ssh` and `rsync` to host `mac` (Tailscale SSH).
- **Layout:** `scripts/mac_test/` (new: state CLI, offload runner, audit, tests) · `scripts/delegate/` (`verify.sh` and its `test_verify_*.py`) · `scripts/production/mac_run.sh` · `scripts/buildlog/` (`record.py`, `index.py`, `report.py`, tests) · `config/` · `commands/`.
- **Key files:**
  - `scripts/delegate/verify.sh` — the one command workers run. `--no-cache` and `--local` are stripped at 558-570; first `cache_lookup` 875-877; cargo token acquire 903-917; `note_event` 636-655; the `test)` arm 1086-1088; usage text is the header comment.
  - `scripts/delegate/test_verify_token_wait.py` — fixture to copy: temp `HOME` with `.claude` symlinked to the repo root, stand-in `cargo` and `git` on `PATH` (lines 34-147), `records()` reads call records (175-178).
  - `scripts/delegate/test_verify_untested_examples.py` — asserts the nextest argv `verify.sh test` builds; it must keep passing unchanged.
  - `scripts/lint/lint` — public entry on the Mac: `lint nextest <args>` (104-105) sources `invoke.sh` and runs `cargo nextest run <args>` as a recorded step.
  - `scripts/lint/sweep.py` — `config_values(path)` (from 1185) reads `key=value` conf files; `repo_name()` (from 1204) names a repository by the directory holding its git common dir.
  - `scripts/lint/memory_admit.py` — 211-216: a record is live only while `/proc/<pid>/stat` start time matches.
  - `scripts/build_hold/build_hold.py` — 635-647: resolve a session's socket with `scripts/message/sessions.py socket session:<id>`, then `scripts/message/send.py --to uds:<socket>`; 718-723: send exit 0 and 1 both count as delivered.
  - `scripts/production/mac_run.sh` — the showrunner's Mac build; reach probe at line 49, `on_mac()` from 40.
  - `scripts/production/test_merge_checkpoint.py` — 44-58, 147: one stand-in script installed as `ssh` on `PATH`, logging argv and printing the remote status as text.
  - `scripts/buildlog/record.py` — `call` fields from env (329-352). `scripts/buildlog/index.py` — `CALL_COLUMNS` from 115, `SCHEMA_VERSION` 33, the `step_days` view (`sum(status != 0) AS failed`) at 258-260. `scripts/buildlog/report.py` — `report(connection, day)` from 1378, `known_crate_steps` 581-602, `rebuilds_section` 677-698.
  - `scripts/buildlog/test_report.py` — fixture 50-57; record builders in `scripts/buildlog/test_index.py` 41-193.
  - `commands/build_hold.md` — model for a short command doc. `config/README.md` — one `## <file name>` heading and a paragraph per config file. `pyrightconfig.json` — `executionEnvironments` with `extraPaths` per script directory.
- **Test lanes:** `scripts/mac_test/` → `scripts/mac_test/test_*.py` (new) · `scripts/delegate/` → `scripts/delegate/test_verify_*.py` · `scripts/buildlog/` → `scripts/buildlog/test_*.py`. Tests sit beside the code and import their sibling by bare name.
- **Build:** none; the code is interpreted.
- **Test:** `python3 -m unittest discover -s <directory> -p '<pattern>'` from the repo root, with the directory and pattern each phase names.
- **Lint:** `basedpyright <directory>`. A pass is its last line `0 errors, 0 warnings, 0 notes`; it exits 3 because `pyrightconfig.json` names a `.venv` no checkout has. Shell files: `bash -n <file>` or `zsh -n <file>`.
- **Invariants:**
  - Any doubt means local. Every Mac-side trouble except the test command's own exit status falls back to today's local run, unchanged. A worker never waits on the Mac longer than the probe timeout.
  - Pass records are written only by the natedev run. A Mac pass never stands as a gate.
  - Never trust ssh's exit status: Tailscale SSH returns 0 whatever the remote command did. Status is a printed `mac_exit=<n>` or `rc=<n>` line; ssh's own 255 or a timeout means unreachable.
  - Tests run nothing on the Mac: stand-in `ssh` and `rsync` on `PATH`, state under `MAC_TEST_STATE_DIR`, config under `MAC_TEST_CONFIG`.
  - On the Mac the feature writes only under `~/.local/state/mac-test/`. Never touch `~/rust/hana`, `~/rust/hana_catalyst_mac`, `~/rust/bevy_hana` or `~/rust/hana_open_freeze_1791356904`.
  - Only step records are written on the Mac. Call records are written on natedev only: `calls.id` is replaced on a second write.
  - Python: basedpyright zero errors and zero warnings, no file-level ignores, no `Any` (a `TypedDict` for each JSON shape).
  - `settings.json` is not edited.
  - `config/mac_test.conf` ships with `offload=off`; only the last phase turns it on.
  - Nothing of this feature writes to or runs on the Mac until the showrunner says the Ian Hubert demo there is over (natedev's call, 2026-10-07): no mirror, copy, test run or measurement. A read-only look is allowed. Every phase before the live check is built and tested on natedev only.
  - Mac facts, read 2026-10-07: `hostname -s` is `Mac`, 12 cores, 64 GiB memory, 187 GiB free, bash 5.3 and `~/.cargo/bin/cargo` on the non-login ssh `PATH`, `/usr/bin/rsync` is Apple's (plain options only).

## Phases

### Phase 1 — The Mac's lock, block and "Mac is free" message  · status: done

#### As-built

- `scripts/mac_test/mac_test.py` is an `argparse` CLI over a state directory (`MAC_TEST_STATE_DIR`, default `~/.local/state/mac-test/`). Every command holds `fcntl.flock` on `state.lock` for its whole read-modify-write; records are written to a temp name, then `os.replace`.
- `run.json`: `{"pid": int, "proc_start": str, "what": str, "worktree": str, "since": "<ISO-8601 UTC>"}`. A run is live only while `/proc/<pid>/stat` field 22 equals `proc_start`. `block.json`: `{"holder": str, "session": str | null, "for": str, "since": str, "state": "pending" | "active"}`, one block at a time, with no `version` key.
- `settle(paths: StatePaths, ended_work: str = "Mac work") -> tuple[RunState, BlockState]` runs first in every command, under the lock: it removes a `run.json` whose process is gone, then turns a `pending` block `active` when no run is live and sends the message. It is the only place a block turns active.
- `claim --pid PID --what TEXT [--worktree PATH] [--wait SECONDS]`: a block of either state prints `blocked by <holder>: <for>`, exit 10; a live run is polled every 2 s up to `--wait` (default 0), then prints `busy: <what> since <HH:MM>`, exit 11; a PID that is not running prints `cannot claim: process <pid> is not running` on stderr, exit 1; otherwise it writes `run.json`, prints `claimed`, exit 0. `--worktree` defaults to the current directory.
- `release --pid PID` removes `run.json` when its pid is PID, then calls `settle(paths, run["what"])`; exit 0 either way.
- `block --holder NAME --for TEXT` takes `session` from `CLAUDE_CODE_SESSION_ID`. No live run: `active`, prints `Mac blocked for <holder>: nothing is running there, it is free now.` A live run: `pending`, prints `Block pending for <holder>: <what> is running on the Mac. Nothing new starts there, and you get a message when it ends.` Another holder's block: `already blocked by <holder>`, exit 1. The same holder again keeps `since` and takes the caller's session and reason.
- `unblock --holder NAME` prints `Mac unblocked.`; another holder's block names the holder, exit 1; no block prints `no block`, exit 0. `status` prints a run line (`free`, or `running: <what> (<worktree>) since <HH:MM zone>`) and a block line (`no block`, `blocked by <holder> since <HH:MM zone>: <for>`, or `block pending for <holder>: <for>`), local zone, exit 0.
- The message is sent once per block, keyed `mac-free-<since>`: `Message from mac-test: the Mac is free. <what> ended at <HH:MM zone>; your block (<for>) is active, and nothing is built or tested there until you run unblock.` With a recorded session, `sessions.py socket session:<id>` gives the target `uds:<socket>`; with no session, or when that lookup fails, the target is the holder's name. It goes through `send.py --to <target> --from mac-test --summary 'Mac is free' --key … --text …`. send.py exit 0 or 1 counts as delivered; any other exit prints `warning: the Mac is free message could not be delivered` on stderr and the block stays active. Both scripts resolve to `Path(__file__).resolve().parent.parent / "message"`; `MAC_TEST_SEND` and `MAC_TEST_SESSIONS` override the paths.

**Files:**
- `scripts/mac_test/mac_test.py` — the lock and block CLI; `state_paths()`, `read_run()`, `read_block()`, `settle()` and the five `command_*` functions.
- `scripts/mac_test/test_mac_test.py` — 9 tests driving the CLI as a subprocess, with stand-in `send` and `sessions` scripts that log their argv and a child process as the run.
- `commands/mac_test.md` — `/mac_test block <why>`, `unblock`, `status`; each runs `python3 ~/.claude/scripts/mac_test/mac_test.py …` with `--holder <session name>`.

**Binds later work:** `claim` exits 10 (blocked) and 11 (busy) are read by the offload runner as decline reasons `blocked` and `busy`, and by `mac_run.sh` in the `verify.sh` wiring as exit 9 with the printed reason. The footer work imports `read_block` and `state_paths`, so both stay importable and free of side effects. A renewal keeps `since`, and the CI-block work keeps that rule. A block record with no `version` key must still decode in the CI-block work. Tests added by the CI-block work set `MAC_TEST_CONFIG` in the shared `setUp`, so these tests never read the live config.

**Gotchas:**
- The message and its `sessions.py` lookup run while `state.lock` is held, and `send.py` can take 40 s; the CI-block work moves every message send and every `gh` call out of that lock.
- `/proc/<pid>/stat` holds the raw process name; it is read with `errors="replace"`, because a non-UTF-8 byte crashes a strict read.
- `basedpyright scripts/mac_test` exits 3 on a clean pass, because `pyrightconfig.json` names a `.venv` no checkout has; the pass is its last line, `0 errors, 0 warnings, 0 notes`.
- An undelivered message leaves no record here: one stderr warning, and `status` is the only place the holder learns the block is active.

**Ruled out:** a revision counter on the block record (every writer of `block.json` holds a lock, so nothing overwrites a renewal); a pending-to-active change inside `release` (it belongs to `settle`); resetting `since` on a repeat block by the same holder; a footer warning for an undelivered "Mac is free" message (the footer already shows pending against active); a footer warning when CI is not configured (a machine-wide config choice, shown by `status`); `status` exiting 2 when `gh` fails (`status` reports, and its switch line says `unknown`); failing `audit --check` on a `needs review` package (natedev still confirms every pass).

### Phase 2 — A block stops CI's Mac job, expires, and names what it skipped  · status: done

#### As-built

- `mac_test.py block` turns hana's `MACOS_CI` variable off through `gh` and records, in the block's `ci` field, one of `off_by_this_block`, `off_before`, `not_configured`, `still_on`. `unblock` and expiry turn the variable back on only for `off_by_this_block`, and list the CI runs the block skipped, per branch. `block` exits 2 and says CI can still use the Mac when `ci` is `still_on`; each later settle tries the switch again.
- A block is pending while a local run is live, or while a queued or in-progress run of `ci_workflow` lists a job named `ci_job` or `ci_gate_job` whose status is not `completed`. When the last one ends, the block becomes active and its holder gets one "Mac is free" message.
- A block lasts `block_hours` unless `--hours` says otherwise, never more than `block_max_hours` (the configured default is held to that limit too). Its holder is warned `block_warn_minutes` before the end, and the block lifts by itself. A lift that cannot turn the switch back on keeps the block, messages the holder hourly and keeps trying.
- Settling runs in a watcher started as a `systemd-run --user` unit named `mac-test-watch-<pid>`, one at a time under `watch.lock`. `state.lock` covers claim and release and never waits on `gh` or a message; `control.lock` covers `block`, `unblock`, `status` and each watcher pass.
- Every `gh` call is bounded by `gh_timeout_s`; the message send and the session lookup by 30 s (`MAC_TEST_MESSAGE_TIMEOUT_S`). A failed session lookup sends to the holder's queue.

**Files:**
- `scripts/mac_test/mac_test.py` — the command: `claim`, `release`, `block`, `unblock`, `status`, `watch`.
- `scripts/mac_test/test_mac_test.py` — 38 tests against stand-ins for `gh`, `systemd-run` and the message scripts.
- `config/mac_test.conf` — `ci_repo`, `ci_variable`, `ci_workflow`, `ci_job`, `ci_gate_job`, `gh_timeout_s`, `block_hours`, `block_max_hours`, `block_warn_minutes`.
- `config/README.md`, `commands/mac_test.md`, `commands/showrunner/produce.md`, `pyrightconfig.json` — one entry or clause each.

**Binds later work:** `read_block(path) -> NoMacBlock | PendingMacBlock | ActiveMacBlock` reads `block.json` (version 2) and raises `ValueError` for a file it cannot decode; `state_paths()` names the state files under `MAC_TEST_STATE_DIR`. A block carries its holder, reason, start, expiry and `ci` word. The footer and the dailies read the block through `read_block` alone, never through `status`. The live check on the Mac settles the CI busy rule from a real run and keeps or removes `ci_gate_job`.

**Gotchas:**
- A missing or empty `ci_repo`, or a missing config file, turns every `gh` call off; a block that had turned the switch off then lifts without `gh`.
- hana's Mac job is listed only after `macOS: Runner Availability` ends, which is why `ci_gate_job` exists. For about a second after that job ends, the busy check can read idle.
- A `gh` set that times out after GitHub applied it is recorded as `still_on`, and the next settle reads `off_before`.
- Tests never assert a short elapsed time: a stand-in holds on a marker file and the test checks order.

**Ruled out:** replacing `ci_gate_job` with "an active run is busy until its Mac job is listed `completed`" before a real run shows how GitHub lists a skipped Mac job.

### Phase 3 — A Mac block shows in the footer and the dailies  · status: done

#### As-built

- A pending or active Mac block is one footer item in every showrunner reply and every dailies report, after the build-hold lines and before the agent lines: `Mac block: <holder> since HH:MM ZZZ, for <reason> - lifts Ddd HH:MM ZZZ`, or `Mac block pending: …` with the same fields. It ends ` - CI can still use the Mac` while the switch-off is not confirmed. It has no unit row and no input key.
- The renderer reads `block.json` directly through `read_block(state_paths().block)`, once per report or footer: no lock, no `gh` call, no message. A file it cannot read shows as `Mac block: its state file cannot be read (<path>)` and never refuses the footer. A reason holding a plumbing word refuses the footer and tells the holder to block again with other words, as a build-hold purpose does.
- A block remembers a switch-off request that got no answer as a fifth `ci` word, `off_unconfirmed`. The next settle reads the switch and records `off_by_this_block` when it is now off. `unblock` and expiry turn the switch back on for `off_by_this_block` and `off_unconfirmed`; `block` exits 2 and warns for `still_on` and `off_unconfirmed`.

**Files:**
- `scripts/production/dailies_render.py` — `UnreadableMacBlock(path)`, `MacBlockFooterState`, `read_mac_block()`, `mac_block_line(block, zone)`, `Report.mac_block`; `footer()` takes the block state.
- `scripts/mac_test/mac_test.py` — `off_unconfirmed`; `switch_ci_off()` returns `off_before`, `off_by_this_block`, `CiSwitchReadFailed(line)` or `CiSwitchWriteUnconfirmed(line)`; `ci_needs_restore(ci)` and `ci_may_still_be_on(ci)`.
- `pyrightconfig.json` — `scripts/production` reads `scripts/mac_test`.
- `commands/showrunner/produce.md`, `commands/showrunner/dailies.md` — the footer item.
- `scripts/production/test_dailies_render_holds.py` and the other `scripts/production` footer and report tests, `scripts/hooks/test_stop_showrunner_footer.py`, `scripts/mac_test/test_mac_test.py` (41 tests).

**Binds later work:** a test that reaches `parse_report` or `footer_main`, in process or through a subprocess, reads the Mac state directory, so it sets `MAC_TEST_STATE_DIR` to a scratch path (a scratch `HOME` does the same). A new `ci` word is added in `CiSwitch`, `decoded_ci`, `ci_needs_restore` and `ci_may_still_be_on`; the renderer asks `ci_may_still_be_on` and holds no list of its own. `claim` still lets an undecodable state file end it with a traceback; the offload runner's phase gives that its own exit code.

**Gotchas:**
- A fixture that copies the command tree copies `scripts/mac_test` and `scripts/lint` too: `mac_test.py` imports `sweep`.
- Turning the switch back on after an unanswered switch-off can undo a switch-off someone made by hand in between; the code says so where it decides.
- A test that patches only `BUILD_HOLD_DIR` still reads the live Mac state.

**Ruled out:** a `status` call from the renderer (it takes a lock and can call `gh`); documenting `--local --no-cache` for a Mac failure, because a Mac failure writes no record and the first lookup had already missed.

### Phase 4 — The offload runner: probe, copy the tree, run on the Mac  · status: done

#### As-built

- `scripts/mac_test/offload.py run --repo-root DIR --package PKG --call-id ID [--filter-run] [--result FILE] -- <nextest words>` tries one nextest run on the Mac. It exits 0 when the run passed there, 75 when the caller must run locally, and the Mac run's own status when tests failed there. `write_result()` writes the exit code's meaning as three lines (`mac=`, `reason=`, `seconds=`) from one `MacOffloadResult` value: `PassedOnMac | FailedOnMac | LostMacRun | DeclinedMacRun`.
- It declines in this order, printing `mac_test: staying on natedev (<reason>)` each time: `off`, `repo`, `linux_only`, `backoff`; then the claim (`blocked`, `busy`, `state`); then the probe (`unreachable`, which also writes the back-off file, `mac_busy`, `battery`, `disk`); then `copy`.
- The copy is `rsync -rlpc --delete --exclude-from` into the mirror, leaving out `/.git`, `/target/` and every ignored path (`git ls-files -z`, with rsync's pattern characters escaped by `escape_rsync_filter_path()`).
- The run is one ssh call through `lint nextest`. Each output line is printed as it arrives; only a line that is exactly `mac_exit=<n>` is held back and read as the status. `mac_skip.<repo>` is added to the `-E` filter.
- Every exit after a possible claim releases it. SIGINT, SIGTERM and SIGHUP stop the ssh child, run `pkill -f <mirror>` on the Mac with a 10 s limit, release the claim and exit 128 plus the signal number.
- A crash inside the runner prints a traceback, removes the result file and exits 75. A run whose status line never arrives is `LostMacRun`: it prints `mac_test: lost the Mac run; running on natedev` and exits 75.
- Every ssh call (probe, run, rsync transport, cleanup) takes its options from `ssh_options(config)`: `BatchMode=yes`, `ConnectTimeout`, `ServerAliveInterval=15`, `ServerAliveCountMax=3`.
- `mac_test.py claim` exits 12 with `state unreadable: <path>` when `run.json` or `block.json` cannot be read.

**Files:**
- `scripts/mac_test/offload.py` — the runner: `OffloadConfig`, `OffloadRequest`, `MacOffloadResult`, `BackoffState = NoBackoff | ActiveBackoff`, `IgnoredPathListingResult = tuple[str, ...] | IgnoredPathListingFailed`, `ssh_options()`, `run_offload()`, `write_result()`.
- `scripts/mac_test/test_offload.py` — its tests, with stand-in `ssh`, `rsync` and `git` first on `PATH`.
- `scripts/mac_test/mac_test.py`, `scripts/mac_test/test_mac_test.py` — `claim`'s exit 12.
- `config/mac_test.conf` — `host`, `connect_timeout_s`, `probe_timeout_s`, `unreachable_backoff_s`, `free_floor_gib`, `max_load`, `mac_budget_gib`, `mac_budget_gib.hana`, `linux_only.hana`, `mac_skip.hana`; `offload=off`.
- `config/README.md` — the entry's new keys.

**Binds later work:** the exit status alone cannot tell a Mac test failure from a runner that was killed or rejected its arguments; only the result file's `mac` word can, so a caller branches on that word and treats a missing, empty or contradictory file as no usable result. A signal ends the Mac's processes; a lost run does not, and the next probe reads `mac_busy` until they end. `mac_budget_gib` is the size limit of the mirror's build folder, passed as `LINT_SWEEP_BUDGET_GIB`; it is not a memory figure. `--filter-run` is accepted and unused inside the runner.

**Gotchas:**
- An unreadable back-off file reads as no back-off; the next failed probe rewrites it.
- A test never asserts a short elapsed time on this machine: a stand-in holds on a marker file and the test checks order.
- `basedpyright scripts/mac_test` prints `0 errors` and still exits 3 in a checkout with no `.venv`.

**Ruled out:** a config key for the keep-alive interval (constants until something needs to change them); a live dead-link test through a TCP proxy, because it needs an alias in the user's ssh configuration; declining as `state` on an unreadable back-off file, because nothing would ever rewrite it.

### Phase 5 — `verify.sh test` tries the Mac first  · status: done

#### As-built

`verify.sh test` strips `--local` into `LOCAL_ONLY`, composes its nextest words in `compose_nextest_args()` before the cargo token, and, when `--local` is absent and the runner file exists, calls `try_mac_test()` between the first cache lookup and the token. `try_mac_test()` sets its `INT`, `TERM` and `HUP` traps, then runs `"$PY" "$MAC_RUNNER" run --repo-root <top level> --package <pkg> --call-id "$BUILDLOG_CALL_ID" --result <temp file> [--filter-run] -- "${NEXTEST_ARGS[@]}"` and reads the file's `mac=`, `reason=` and `seconds=` lines:
- `declined` and `lost` run the test on natedev as before.
- No usable result (missing, empty, no single known `mac` word, `passed` with a nonzero status, `failed` with status 0) is recorded as `declined` with reason `runner` and runs on natedev.
- A full `passed` is confirmed by the natedev run in the same call.
- A `passed` on a `--filter` run prints its line, records `passed_filter` and exits 0.
- `failed` records the call and exits with the runner's status.
- A signal is forwarded to the runner and waited for, the result file is removed, and the exit is 128 plus the signal number; the handler is correct while no runner has started.

`--local` records `declined` with reason `local_flag`. Every call record carries `mac`, `mac_reason` and `mac_s` from `BUILDLOG_MAC`, `BUILDLOG_MAC_REASON` and `BUILDLOG_MAC_S`; `mac_s` is null unless the value is a finite number of zero or more.

`mac_run.sh` claims the Mac after its reach probe (`mac_test.py claim --pid $$ --what "mac run <9 characters of the sha>" --wait 600`). Exit 10, 11 or 12 from the claim prints the claim's line and exits 9; exit 3 stays "Mac unreachable". A normal exit releases through the `EXIT` trap. `INT`, `TERM` and `HUP` run `stop_mac_run`: one ssh call (`-o ConnectTimeout=6 -o BatchMode=yes`) that ends each `cargo build` whose working directory is the clone, ends processes from the clone's `target/debug`, and removes `/tmp/mac_run.bundle`, `/tmp/mac_run_hana.command` and `/tmp/mac_run_hana.status`; then the release, then exit 128 plus the signal number, whatever the cleanup call returned.

`offload.py`: a lost run prints `mac_test: the link to the Mac dropped; running on natedev instead`; `run` puts an inherited ignored `SIGINT` back to its default; once cleanup has begun, repeated `INT`, `TERM` and `HUP` are ignored and the exit status is the first signal's.

**Files:**
- `scripts/delegate/verify.sh` — the `--local` word, `compose_nextest_args()`, `try_mac_test()`, `forward_mac_runner_signal()`, the three `BUILDLOG_MAC*` values passed to `record.py call`.
- `scripts/delegate/test_verify_mac_offload.py` — the Mac step against a stand-in runner (`VERIFY_MAC_RUNNER`) and a stand-in `cargo`.
- `scripts/production/mac_run.sh` — claim, exit 9, release, `stop_mac_run`.
- `scripts/production/test_mac_run.py` — `mac_run.sh` under a temporary `HOME` and `TMPDIR` with stand-in `ssh`, `scp` and `git`.
- `scripts/buildlog/record.py`, `scripts/buildlog/test_record.py` — the three Mac fields and `optional_nonnegative_number()`.
- `scripts/mac_test/offload.py`, `scripts/mac_test/test_offload.py` — the lost line, the interrupt edges, `default_target_budget_gib`.
- `commands/showrunner/produce.md` — exit 9 in the Mac run rule.
- `commands/unit/direct.md` — the `--local` row of the verification table (moved from `commands/unit/delegate.md` when that command was renamed `/unit:direct`).
- `config/README.md`, `config/mac_test.conf` — `mac_budget_gib` described as the size limit of the mirror's build folder.

**Binds later work:** the build-log index and report read `mac` (`passed`, `passed_filter`, `failed`, `lost`, `declined`), `mac_reason` (the runner's twelve reasons plus `local_flag` and `runner`) and `mac_s` from each call record. The live checks on the Mac prove `stop_mac_run`'s cleanup call and the runner's interrupt cleanup against real macOS. `VERIFY_MAC_RUNNER` names the runner a test substitutes.

**Gotchas:**
- A non-interactive bash starts an `&` child with `SIGINT` ignored, and Python keeps an inherited ignore; without the restore in `offload.py run` a forwarded interrupt is dropped and `verify.sh` waits out the whole Mac run.
- A terminal interrupt reaches the runner twice: once through the process group, once forwarded.
- `stop_mac_run`'s cleanup call uses the Mac's `pgrep`, `lsof`, `pkill`, `kill`, `sed` and `rm`, and has only run against a stand-in `ssh`.
- A signal exit during the Mac step writes no call record.
- The lock lives on natedev (`~/.local/state/mac-test`), tied to the claiming process; a job started on the Mac cannot take it.
- `basedpyright` exits 3 in a checkout with no `.venv`; its last line is the result.

**Ruled out:** a call record on a signal exit; treating `declined` or `lost` with runner status 0 as a contradiction; a claim taken by `verify.sh` itself (the runner claims).

### Phase 6 — Which packages are pointless on the Mac  · status: done

#### As-built

`scripts/mac_test/audit.py <repo dir> [--check]` lists, for a Rust workspace, the packages whose tests depend on the operating system. It reads the root `Cargo.toml` with `tomllib`, expands `[workspace] members` globs, drops `exclude` entries and runs no cargo. It prints a markdown table, `Package | Gated sites (src) | Gated sites (tests) | Target dependencies | Listed | Suggestion`, largest counts first.

- **Gated sites.** Predicates are read in `src/`, `tests/` and `build.rs` from `cfg(…)`, `cfg!(…)` and the first argument of `cfg_attr(…)`, across lines; the site is the line where the predicate opens. A `target_os` or `target_family` word outside every predicate is one site whose trimmed line is an unclassified predicate.
- **Target dependencies.** The keys of the member's `[target.'cfg(…)'.dependencies]` and `dev-dependencies` tables.
- **Listed.** `yes` or `no` from `linux_only.<repo>` in `MAC_TEST_CONFIG`, read with `config_values()` from `scripts/lint/sweep.py`; the repository is `KnownRepository(name) | UnknownRepository` from `offload.py`. With an unknown repository every row reads `unknown` and one line says the directory is in no git repository.
- **Suggestion.** Each predicate is *Linux only* (`target_os = "linux"`, `not(target_os = "macos")`), *same on both* (`unix`, `windows`, `not(unix)`, `not(windows)`, `target_family = "unix"`), *macOS only* (`target_os = "macos"`) or *unclassified* (any other form, every compound `any(…)` and `all(…)` included). A member with a Linux-only predicate on a target dependency table is `keep on natedev`; else one with an unclassified predicate is `needs review`; else `can go to the Mac`.
- **Lines under the table.** `Listing disagreements:` names each package whose suggestion and listing disagree; `Needs review:` names each such package with its unclassified predicates; `Not built on the Mac:` names, for each package not kept on natedev, its Linux-only lines as `path:line` relative to the repo dir, or `none.`.
- **`--check`** exits 1 when a `keep on natedev` package is not listed, and 2 for an unknown repository or an unreadable manifest. A `needs review` package is printed and does not fail it.

`commands/mac_test.md` documents `audit <repo dir>` and holds the section `Taking the Mac for a job of your own`, for a job that reaches the Mac over ssh without `verify.sh test` or `mac_run.sh`: `mac_test.py claim --pid $$ --what "<what it is>" --wait <seconds>` (exit 10 blocked, 11 busy, 12 unreadable state), then `mac_test.py release --pid $$`. The lock lives on natedev and is tied to the claiming process, so a job that dies frees it and a job started on the Mac cannot take it.

**Files:**
- `scripts/mac_test/audit.py` — the audit command.
- `scripts/mac_test/test_audit.py` — subprocess tests over temp workspaces: the table and its three lines, `--check`, multi-line and `cfg_attr` predicates, the unknown repository.
- `commands/mac_test.md` — the `audit` argument and the section on taking the lock from a job of one's own.

**Binds later work:** the audit's table, its `--check` exit statuses and its `Not built on the Mac:` line are what the live run on hana reads before it settles the Linux-only list. `verify.sh test` and `mac_run.sh` take the Mac's lock themselves, so a caller that claims first and then calls either is reported busy.

**Gotchas:**
- Gating is at compile time: a gated test is not built on the Mac, so a filter that names only such a test matches no test there and nextest exits 4.
- hana writes OS conditions as `cfg_attr` and over several lines; a reader of single lines undercounts and can clear a package wrongly.
- A package whose tests all sit behind Linux-only source lines still reads `can go to the Mac`; its `Not built on the Mac:` entry is the only sign.
- Measured on hana, 2026-10-07: `hana` can go to the Mac with six lines not built there; `hana_video` and `hana_clerestory` keep on natedev and are listed; `hana_prosody` is listed yet reads `can go to the Mac` (its one target dependency, `speech`, is macOS only); `hana_rubric` (`all(test, unix)`) and `hana_liminal` (`target_arch = "wasm32"`) need review.

**Ruled out:** keeping a package off the Mac for a Linux-only source or test line — it kept `hana`, the package the Mac helps most, on natedev.

### Phase 7 — The build log and the report show what the Mac took  · status: done

#### As-built

- The build log index is at schema 10. `calls` carries `mac` (`passed`, `passed_filter`, `failed`, `lost`, `declined`), `mac_reason` and `mac_s`; records written before them read as null.
- `offload.py`: `NoTestMatchedOnMac(seconds)` is a member of `MacOffloadResult`. When the Mac's nextest exits 4, the result is written as `mac = "declined"`, `reason = "no_tests"`, runner status 75, with the line `mac_test: no test matched on the Mac; running on natedev instead`, and `verify.sh` continues on natedev. The remote command sets `BUILDLOG_CALLER=verify-mac`, so the Mac's steps carry that caller.
- `report.py` filters natedev's tables in two ways. `OFFLOADED_STEP` is `caller IS 'verify-mac'`, and `natedev_step_predicate(step="steps")` leaves those steps out of every step query. `natedev_call_predicate(call="calls")` leaves out only a call with `mac` of `failed` or `passed_filter`; every other call went on to natedev and stays in. `call_ran_on_natedev(call="calls")` is true when the call has a `nextest` step of its own on its host; it serves `natedev p50` and the `--local` pairing.
- `offload_record(mac, reason, seconds_value)` decodes a call's three columns into `NoOffloadRecord | LocalRunRequested | RunnerResultUnavailable | OffloadDeclined(reason, seconds) | MacRunCompleted(outcome, seconds) | MacRunLost(seconds) | MacRunMatchedNoTest(seconds) | UnrecognizedOffloadRecord`. An unrecognized word is counted nowhere; the raw columns stay queryable.
- `mac_tests_section(connection, day)` prints `Tests on the Mac` after the test builds section: a table `Package | Tried | Failed on the Mac | Passed there | Mac p50 | natedev p50 | Stayed local`, a `Source:` line, `natedev runs avoided: N — about X min of build and test time and Y min of waiting, estimated from that day's natedev runs of the same packages`, a line for the Mac failures a `--local` rerun answered, and a `No test matched on the Mac:` line. A no-test run counts as tried.

**Files:**
- `scripts/buildlog/index.py` — the three call columns and schema 10.
- `scripts/buildlog/report.py` — the two filters, the decoded record and the section.
- `scripts/mac_test/offload.py` — the no-test result and the `verify-mac` caller label.
- `scripts/buildlog/test_index.py`, `scripts/buildlog/test_report.py`, `scripts/mac_test/test_offload.py`, `scripts/delegate/test_verify_mac_offload.py` — their tests.

**Binds later work:** the live check reads the Mac's step in natedev's index by `caller = 'verify-mac'` after `buildlog sync`; the report reads natedev's index only. A Mac no-test step keeps raw status 4, and the index's `step_days` view counts every nonzero step as failed.

**Gotchas:**
- With the runner in place every `verify.sh test` call carries a Mac word, `off` and `repo` included, and `verify.sh` sets `local_flag` before its first recorded-pass lookup. A rule keyed on "has a Mac word" touches every call.
- A natedev step can lack its call record (two in the live index on 2026-10-07), so a missing call record never marks a Mac step.
- `off` and `repo` are listed in no column and create no row. A package has a row only with a tried run or one of the other ten decline reasons, and with no row the section is absent: with `offload=off` the report says nothing about the Mac.
- The `--local` pairing matches a failed Mac call to a `local_flag` pass of the same worktree, package and command within 30 minutes, and reads 30 minutes past the report day; such a next-day call counts in nothing else.
- The waiting estimate is build-token wait alone.

**Ruled out:** a row for a package whose only Mac words are `local_flag` or `runner` (it would show nothing); marking a Mac step by a missing call record (natedev steps can lack one too).

### Phase 8 — Live on the Mac, then switched on  · status: done

#### As-built

- Mac test offload is on. `config/mac_test.conf` sets `offload=on`, `mac_budget_gib.hana=56` (the build folder measured 42.18 GiB with both suites run and nothing trimmed) and `free_floor_gib=100`. `linux_only.hana` is `hana_video,hana_clerestory`: `hana_prosody` passes on the Mac. `mac_skip.hana` is `test(world_panel_words_match_drawn_pixels)`, which fails only on the Mac and is still gated on natedev, where every full Mac pass is confirmed.
- `ci_activity` treats a queued or in-progress `ci_workflow` run as busy until it lists a job named `ci_job` with status `completed`. GitHub lists the macOS job only after `macOS: Runner Availability` completes, and lists it at once as completed/skipped while the switch is off. `ci_gate_job` no longer exists. A block waiting on CI says `a CI run may still use the Mac`, and its free message says `CI's hold on the Mac ended`; the stored `waiting_on` kind stays `ci_job`.
- `claimed_run`: a `MissingMacStatus` calls `end_remote_work` (the same bounded `pkill -f <mirror>` ssh that a signal runs), then returns `LostMacRun` with the elapsed time taken after that cleanup.
- The build log index is at schema 11. `COUNTED_FAILURE` (`status != 0 AND NOT (caller IS 'verify-mac' AND step IS 'nextest' AND status = 4)`) drives `step_days.failed` and the `failures` view, so a Mac "no tests to run" keeps raw status 4 in `steps` without counting as a failure.
- Measured on hana `46b8c87e5`: Mac cold build 12 min 35 s against natedev's 5 min 39 s. A rebuild after a content edit to `crates/hana/src/tool/surface.rs` takes 37.28 s on the Mac (tree copy 4.2 s) against 52.05 s on natedev.

**Files:**
- `config/mac_test.conf`, `config/README.md` — the switch, the measured limits and the package lists.
- `scripts/mac_test/mac_test.py` — the CI busy rule and the waiting text.
- `scripts/mac_test/offload.py` — remote cleanup on a lost run.
- `scripts/buildlog/index.py` — `COUNTED_FAILURE` and schema 11.
- `scripts/mac_test/test_mac_test.py`, `scripts/mac_test/test_offload.py`, `scripts/buildlog/test_index.py` — their tests.

**Gotchas:**
- The mirror copy is `rsync -rlpc`: it compares checksums and carries no modification times, so a touch-only edit never reaches the Mac. Time a rebuild with a content edit.
- The probe counts every `cargo`, `rustc` and `cargo-nextest` process on the Mac, so leftovers from a lost run whose cleanup could not reach the Mac make the next run decline as `mac_busy`.
- A pending block also waits on queued runs that have not yet reached their runner-availability job; that wait is the cost of the single-job rule.

**Ruled out:** keeping a lost run's claim when its cleanup ssh fails (the probe already declines the next run while leftovers live); the two-job rule with `ci_gate_job` (the live runs showed one job is enough).
