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
  - `scripts/delegate/verify.sh` — the one command workers run. `--no-cache` is stripped at 493-502; first `cache_lookup` 700-702; cargo token acquire 721-735; `note_event` 561-580; the `test)` arm 904-966; usage text is the header comment 70-111.
  - `scripts/delegate/test_verify_token_wait.py` — fixture to copy: temp `HOME` with `.claude` symlinked to the repo root, stand-in `cargo` and `git` on `PATH` (lines 34-147), `records()` reads call records (175-178).
  - `scripts/delegate/test_verify_untested_examples.py` — asserts the nextest argv `verify.sh test` builds; it must keep passing unchanged.
  - `scripts/lint/lint` — public entry on the Mac: `lint nextest <args>` (104-105) sources `invoke.sh` and runs `cargo nextest run <args>` as a recorded step.
  - `scripts/lint/sweep.py` — `config_values(path)` (1174-1190) reads `key=value` conf files; `repo_name()` (1193-1222) names a repository by the directory holding its git common dir.
  - `scripts/lint/memory_admit.py` — 211-216: a record is live only while `/proc/<pid>/stat` start time matches.
  - `scripts/build_hold/build_hold.py` — 635-647: resolve a session's socket with `scripts/message/sessions.py socket session:<id>`, then `scripts/message/send.py --to uds:<socket>`; 718-723: send exit 0 and 1 both count as delivered.
  - `scripts/production/mac_run.sh` — the showrunner's Mac build; reach probe at line 45, `on_mac()` 36-43.
  - `scripts/production/test_merge_checkpoint.py` — 44-58, 147: one stand-in script installed as `ssh` on `PATH`, logging argv and printing the remote status as text.
  - `scripts/buildlog/record.py` — `call` fields from env (320-340; `token_wait_s` at 333). `scripts/buildlog/index.py` — `calls` columns 107-141, old-record default 381, `SCHEMA_VERSION` 33. `scripts/buildlog/report.py` — `report(connection, day)` 951-975, `known_crate_steps` 378-384, `rebuilds_section` 473-494.
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

### Phase 4 — The offload runner: probe, copy the tree, run on the Mac  · status: todo

#### Work Order

**Goal:** One command takes a nextest argv and either runs it on the Mac against a copy of the worktree, streaming the output back, or says in one word why the run stays local.

**Spec:**
New `scripts/mac_test/offload.py`:
`offload.py run --repo-root DIR --package PKG --call-id ID [--filter-run] [--result FILE] -- <words after "cargo nextest run">`

Exit codes: `0` passed on the Mac; `75` not run there (declined or lost; the run must happen locally); any other value is the Mac run's own exit status. With `--result`, write three lines: `mac=<passed|failed|lost|declined>`, `reason=<word or empty>`, `seconds=<float>`. A decline prints one line `mac_test: staying on natedev (<reason>)`. Those three lines are a wire form for `verify.sh` only. Inside `offload.py` the outcome is one value, `MacOffloadResult = PassedOnMac(seconds) | FailedOnMac(status, seconds) | LostMacRun(seconds) | DeclinedMacRun(reason: MacDeclineReason, seconds)`, with `MacDeclineReason` a `Literal` of the decline words below (`off`, `repo`, `linux_only`, `backoff`, `blocked`, `busy`, `state`, `unreachable`, `mac_busy`, `battery`, `disk`, `copy`); the exit code and the result file are both written from it by one function, `write_result()`, so no reason can travel with a pass and no status with a decline.

Config: `MAC_TEST_CONFIG`, default `~/.claude/config/mac_test.conf`, read with `config_values()` imported from `scripts/lint/sweep.py` (the import form of `scripts/buildlog/disk.py:15-16`). `config/mac_test.conf` gains these keys, each commented in plain words:
```
offload=off
host=mac
connect_timeout_s=6
probe_timeout_s=15
unreachable_backoff_s=300
free_floor_gib=60
max_load=6
mac_budget_gib=24
mac_budget_gib.hana=64
linux_only.hana=hana_video,hana_clerestory,hana_prosody
mac_skip.hana=
```
A repository is offloadable only when a `linux_only.<repo>` key exists, empty or not. `<repo>` comes from `repo_name(<root>)` in `scripts/lint/sweep.py`, which reads the `.git` files without calling git and returns `str | None`. Turn that into `KnownRepository(name) | UnknownRepository` where it is read; an unknown repository declines as `repo`.

Steps, in order; each failure is a decline with the reason shown:
1. `offload` is not `on` → `off`. No `linux_only.<repo>` key → `repo`. PKG in that list → `linux_only`.
2. `unreachable_until` in the state directory is in the future → `backoff`.
3. `mac_test.py claim --pid <own pid> --what "nextest <PKG>" --worktree <root>`: exit 10 → `blocked`, 11 → `busy`, 12 or any other nonzero status → `state` (the Mac's coordination state cannot be read; no ssh or rsync call follows). From here on, always run `mac_test.py release --pid <own pid>` before exiting, on signals too.
4. Probe, one ssh call: `ssh -o BatchMode=yes -o ConnectTimeout=<connect_timeout_s> <host> '<script>'` under `probe_timeout_s`. The script makes the mirror directory `.local/state/mac-test/mirror/<repo>` and prints `procs=<count of cargo, rustc and cargo-nextest processes of any user>`, `load=<1-minute load>`, `power=<ac|battery>`, `free_gib=<free GiB on the home volume>`, `host=<hostname -s>`, `rc=0`, using `pgrep -x`, `sysctl -n vm.loadavg`, `pmset -g batt` and `df -g "$HOME"`. ssh exit 255, a timeout or no `rc=0` line → write `unreachable_until = now + unreachable_backoff_s`, reason `unreachable`. `procs` above 0 or `load` above `max_load` → `mac_busy`. `power=battery` → `battery`. `free_gib` below `free_floor_gib` → `disk`.
5. Copy: write an exclude file holding `/.git`, `/target/` and one anchored line per path from `git -C <root> ls-files --others --ignored --exclude-standard --directory`. Then `rsync -rlpc --delete --exclude-from=<file> -e "ssh -o BatchMode=yes -o ConnectTimeout=<n>" <root>/ <host>:.local/state/mac-test/mirror/<repo>/`. No `-t` and no `-a`: a file whose content is unchanged keeps its Mac modification time, so cargo rebuilds only what changed, whichever worktree the copy came from. A nonzero rsync status → `copy`.
6. Run: print `mac_test: running on the Mac (macOS, host <host>); tree copied in <s> s`. Then one ssh call whose remote command is, built with `shlex.join` and `shlex.quote`:
   `cd <mirror> && PATH="$HOME/.cargo/bin:$PATH" BUILDLOG_CALLER=verify BUILDLOG_CALL_ID=<ID> BUILDLOG_SYNC=1 LINT_SWEEP_BUDGET_GIB=<mac_budget_gib.<repo>, else mac_budget_gib> ~/.claude/scripts/lint/lint nextest <words>; echo mac_exit=$?`
   When `mac_skip.<repo>` is not empty, the value after `-E` becomes `(<given>) & not (<mac_skip>)`. Stream the remote stdout and stderr line by line as they arrive; hold back the last `mac_exit=<n>` line. No such line → `lost`, exit 75. `mac_exit=0` → exit 0. Otherwise print `mac_test: this ran on the Mac (macOS). A failure that looks unrelated to your change may be a macOS difference; rerun with --local to run it on natedev.` and exit `<n>`.
7. On SIGTERM or SIGINT: stop the ssh child, try once for at most 10 s to end what it started (`ssh <host> "pkill -f <mirror>"`), release, and exit 128 plus the signal number.

The `## mac_test.conf` entry in `config/README.md` gains the new keys.

`scripts/mac_test/mac_test.py`: `claim` catches a state file that cannot be read or decoded (`ValueError` or `OSError` from `live_run` or `read_block`), prints `state unreadable: <path>` and exits 12. Today that error leaves `command_claim` as a traceback. `commands/mac_test.md` is unchanged: `claim` is not a user command.

**Files:**
- `scripts/mac_test/offload.py` — new: the runner.
- `scripts/mac_test/test_offload.py` — new: its tests.
- `config/mac_test.conf` — the offload keys.
- `config/README.md` — the entry's new keys.
- `scripts/mac_test/mac_test.py` — `claim` exit 12 for an unreadable state.
- `scripts/mac_test/test_mac_test.py` — its test.

**Seats:** `1 writer + 1 tester` — the runner and its config are one writer's; the tests come from this Spec.
- `impl` — `scripts/mac_test/offload.py`, `config/mac_test.conf`, `config/README.md`, `scripts/mac_test/mac_test.py`
- `test` — `scripts/mac_test/test_offload.py`: one stand-in script installed as `ssh` and `rsync` on `PATH` (the form in `scripts/production/test_merge_checkpoint.py:44-58`), logging argv, with knobs for each probe line, `mac_exit=<n>`, ssh's own 255, a slow answer and a missing last line. Also `scripts/mac_test/test_mac_test.py`: `claim` against a damaged `block.json` exits 12 with its one line.

**Constraints from prior phases:** `scripts/mac_test/mac_test.py` exists with `claim` (0 claimed, 10 blocked, 11 busy) and `release`, state under `MAC_TEST_STATE_DIR`. Both take only the short state lock, never call `gh` and never send a message, so neither can stall a worker; `release` under a pending block starts the block watcher through `systemd-run`, so tests put a stand-in `systemd-run` on `PATH` or leave no block in place. `config/mac_test.conf` exists with the `ci_*`, `gh_timeout_s` and `block_*` keys, read through `MAC_TEST_CONFIG` with `config_values()`; `pyrightconfig.json` already lists `scripts/mac_test` with `scripts/lint` on its path.

**Acceptance gate:** `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` ends `OK`; `basedpyright scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`. Tests prove: each decline reason, with exit 75 and the result file; a second call inside the back-off makes no ssh call; the rsync argv and the exclude file; the remote command carries the call id, the budget and the exact nextest words, with a filter such as `package(hana) & (test(a) | test(b))` surviving quoting; `mac_skip` wraps the filter; output order is kept and the `mac_exit` line is not shown; exit 0, a failing status and a lost connection; the claim is released on every path, a signal included; a damaged state file declines as `state` with no ssh and no rsync call; a directory in no git repository declines as `repo`.

### Phase 5 — `verify.sh test` tries the Mac first  · status: todo

#### Work Order

**Goal:** A worker's `verify.sh test <package>` fails fast on the Mac when the Mac is free, a pass there is confirmed on natedev, and the showrunner's Mac build honours the same lock and block.

**Spec:**
`scripts/delegate/verify.sh`:
- `--local` is stripped beside `--no-cache` (493-502) into `LOCAL_ONLY=1`, so it is in neither the tree key nor the call record's words. Add its usage line to the header comment (70-111).
- For `test`, the nextest words are composed before the cargo token is taken. Move the `test)` arm's parsing and target selection (905-966) into a function that fills `NEXTEST_ARGS` (the words after `cargo nextest run`) and runs after the first `cache_lookup` (700-702); the arm then only calls `run_nextest "${NEXTEST_ARGS[@]}"`. The local argv stays byte-identical to today's.
- Between that and the token acquire (721-735), when the first lookup missed and `LOCAL_ONLY` is 0, call the runner: `"$PY" "$MAC_RUNNER" run --repo-root "$(git rev-parse --show-toplevel)" --package "$PKG" --call-id "$BUILDLOG_CALL_ID" --result <temp file> [--filter-run] -- "${NEXTEST_ARGS[@]}"`, its output going straight to the caller. `MAC_RUNNER` is `${VERIFY_MAC_RUNNER:-$HOME/.claude/scripts/mac_test/offload.py}`; a missing file skips the step.
  - Exit 75: continue into the local path, unchanged.
  - Exit 0 on a `--filter` run: print `verify.sh: PASS on the Mac (macOS). A filtered run is feedback, so nothing ran on natedev.`, write the call record with `BUILDLOG_MAC=passed_filter`, exit 0. No pass record.
  - Exit 0 otherwise: print `verify.sh: passed on the Mac (macOS); now confirming on natedev, which is the gate.` and continue into the local path.
  - Any other status: write the call record with that status and exit with it. No token, no local run, no pass or fail record.
- `note_event` (561-580) passes `BUILDLOG_MAC`, `BUILDLOG_MAC_REASON` and `BUILDLOG_MAC_S` to `record.py call` beside `BUILDLOG_TOKEN_WAIT_S` (577), read from the result file; `BUILDLOG_MAC` is the file's `mac` word, except `passed_filter` for the filtered pass above, the one pass that no natedev run follows. `--local` records `declined` with reason `local_flag`. `record.py` ignores them until the build-log phase lands.
- An interrupt while the runner is active reaches the runner, which releases its claim. The runner starts before the traps at 885-890 are set, so it runs as a background child that `verify.sh` waits for under `INT`, `TERM` and `HUP` traps of its own: each forwards the signal to the runner, waits for it to end, and exits 128 plus the signal number with no token taken and no local cargo run.
- No other verb calls the runner; `final` stays on natedev.

`scripts/production/mac_run.sh`: after the reach probe (line 45), `python3 ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ --what "mac run ${sha[1,9]}" --wait 600`. Exit 10, 11 or 12: print the line it gave (`blocked by <holder>: <for>`, `busy: <what> since <HH:MM>` or `state unreadable: <path>`) and exit 9, a new code that means the Mac is blocked, busy with a test, or its coordination state cannot be read. Exit 3 keeps meaning only that the Mac is unreachable, which hana's showrunner reads as an outage (natedev, 2026-10-07). Release on every exit path. Update the header's exit list.

`commands/showrunner/produce.md`, the Mac run rule (505-512): add that exit 9 means the Mac is blocked, busy with a test, or its coordination state cannot be read, so the run is skipped and it is no outage. `commands/unit/delegate.md`, the `<VerificationContract/>` table: add the row `a Mac failure that looks unrelated to the change, run on natedev` → `bash ~/.claude/scripts/delegate/verify.sh test <package> --local`.

**Files:**
- `scripts/delegate/verify.sh` — `--local`, the composed argv, the Mac step, the call record fields.
- `scripts/delegate/test_verify_mac_offload.py` — new.
- `scripts/production/mac_run.sh` — claim and release.
- `scripts/production/test_mac_run.py` — new: the claim, exit 9 and the release.
- `commands/showrunner/produce.md` — one clause in the Mac run rule.
- `commands/unit/delegate.md` — one table row.

**Seats:** `1 writer + 1 tester` — one writer holds the scripts and docs; the tests come from this Spec.
- `impl` — `scripts/delegate/verify.sh`, `scripts/production/mac_run.sh`, `commands/showrunner/produce.md`, `commands/unit/delegate.md`
- `test` — `scripts/delegate/test_verify_mac_offload.py`: the fixture of `test_verify_token_wait.py` (its stand-in `cargo` extended to answer `nextest --version` and `nextest run`), with `VERIFY_MAC_RUNNER` pointing at a stand-in runner that logs its argv, writes the result file and exits as told. `scripts/production/test_mac_run.py`: `mac_run.sh` run with a temp `HOME` whose `.claude` links to the repo root, stand-in `ssh`, `scp` and `git` on `PATH` (the form in `scripts/production/test_merge_checkpoint.py:44-58`), and the real `mac_test.py` under `MAC_TEST_STATE_DIR` and `MAC_TEST_CONFIG` on scratch paths (`ci_repo=` empty and a stand-in `systemd-run` on `PATH`): a block placed with `mac_test.py block` gives 10, a claim held by the test's own process gives 11, a damaged `block.json` gives 12. `mac_run.sh` names the script as `$HOME/.claude/scripts/mac_test/mac_test.py`, so under the linked `.claude` no stand-in can take its place.

**Constraints from prior phases:** `scripts/mac_test/offload.py run` exits 0 (Mac pass), 75 (run locally) or the Mac run's status, and writes `mac=`, `reason=`, `seconds=` lines to `--result`. `scripts/mac_test/mac_test.py claim --wait` exits 0, 10 (prints `blocked by <holder>: <for>`), 11 (prints `busy: <what> since <HH:MM>`) or 12 (prints `state unreadable: <path>`); the runner's decline reasons include `state` for that last one. `config/mac_test.conf` ships `offload=off`, so nothing reaches the Mac yet.

**Acceptance gate:** `python3 -m unittest discover -s scripts/delegate -p 'test_verify_*.py'` ends `OK` with `test_verify_untested_examples.py` unchanged; `basedpyright scripts/delegate` ends `0 errors, 0 warnings, 0 notes`; `bash -n scripts/delegate/verify.sh` and `zsh -n scripts/production/mac_run.sh` pass; `python3 -m unittest discover -s scripts/production -p 'test_mac_run.py'` ends `OK`; `basedpyright scripts/production` ends `0 errors, 0 warnings, 0 notes`. `mac_run.sh` tests prove: a claim answering 10, 11 or 12 prints the line it gave and exits 9 with no build started; an unreachable Mac still exits 3; the claim is released after a pass, after an early failure and after SIGTERM. `verify.sh` tests prove: the runner gets the same words the local run would; exit 75 runs locally with the token as before; a Mac failure exits with that status, runs no local cargo and writes no pass record; a Mac pass on a full run then runs locally and only that run writes the pass record; a Mac pass on a `--filter` run ends there; `--local` never calls the runner and a recorded pass answers both forms; `check`, `lint` and `final` never call it; the call record carries the three fields, with `passed_filter` for a filtered Mac pass; a SIGTERM sent to `verify.sh` while the runner is active reaches the runner (it writes a marker), and no token is taken and no local cargo starts.

### Phase 6 — Which packages are pointless on the Mac  · status: todo

#### Work Order

**Goal:** One command lists, for any Rust workspace, the packages whose tests depend on the operating system, so the Linux-only list can be checked.

**Spec:**
New `scripts/mac_test/audit.py <repo dir> [--check]`. It reads the root `Cargo.toml` with `tomllib`, expands `[workspace] members` globs and drops `exclude` entries; it runs no cargo. For each member:
- **gated sites:** lines in `src/`, `tests/` and `build.rs` matching `target_os`, `cfg(unix)`, `cfg(windows)`, `target_family`, counted for source and tests apart;
- **target dependencies:** the keys of the member's `[target.'cfg(…)'.dependencies]` and `dev-dependencies` tables;
- **listed:** whether `linux_only.<repo>` in `MAC_TEST_CONFIG` names it;
- **suggestion**, from every predicate found, in gated sites and target tables alike. Each predicate is one of three kinds: *Linux only*, true on Linux and false on macOS (`target_os = "linux"`, `not(target_os = "macos")`); *same on both* (`unix`, `target_family = "unix"`, `not(windows)`, `windows`) or *macOS only* (`target_os = "macos"`), neither of which keeps a package off the Mac; and *unclassified*, any other form, a compound `any(…)` or `all(…)` included. A member with a Linux-only predicate is `keep on natedev`; else one with an unclassified predicate is `needs review`; else `can go to the Mac`.

Output is a markdown table: `Package | Gated sites (src) | Gated sites (tests) | Target dependencies | Listed | Suggestion`, largest counts first, then one line naming each package whose suggestion and listing disagree, and one naming each `needs review` package with its unclassified predicates. The repository name comes from `repo_name(<repo dir>)` in `sweep.py` as `KnownRepository(name) | UnknownRepository`, the type `offload.py` defines. With an unknown repository, `Listed` reads `unknown` for every member, one line under the table says the directory is in no git repository, and `--check` exits 2. `--check` exits 1 when a `keep on natedev` package is not listed; a `needs review` package is printed and does not fail it, because natedev still confirms every pass. `commands/mac_test.md` gains `audit <repo dir>` with one line on reading the table: gating is at compile time, so a gated test does not fail on the Mac, it is simply not built there.

**Files:**
- `scripts/mac_test/audit.py` — new.
- `scripts/mac_test/test_audit.py` — new.
- `commands/mac_test.md` — the `audit` argument.

**Seats:** `1 writer + 1 tester` — one small command; the tests come from this Spec.
- `impl` — `scripts/mac_test/audit.py`, `commands/mac_test.md`
- `test` — `scripts/mac_test/test_audit.py`: a temp workspace of five members (a linux target table, a Linux-gated test only, a macOS-only dependency, a compound predicate, and one plain), member globs and `exclude`, the table with all three suggestions, both lines under it and `--check`. The temp workspace holds a `.git` directory, so its repository name is the temp directory's; one more case runs without it and proves `unknown` and exit 2.

**Constraints from prior phases:** `config/mac_test.conf` holds `linux_only.<repo>=<comma list>`; read it with `config_values()` from `scripts/lint/sweep.py`, as `offload.py` does. `offload.py` defines `KnownRepository(name) | UnknownRepository`, built from `sweep.repo_name()`, which returns `str | None` and calls no git. `commands/mac_test.md` exists with `block`, `unblock`, `status`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` ends `OK`; `basedpyright scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`.

### Phase 7 — The build log and the report show what the Mac took  · status: todo

#### Work Order

**Goal:** The build report says how many test runs the Mac took, how they went, why others stayed local, and how much natedev time and waiting that removed.

**Spec:**
- `scripts/buildlog/record.py call`: read `BUILDLOG_MAC`, `BUILDLOG_MAC_REASON`, `BUILDLOG_MAC_S` into fields `mac`, `mac_reason`, `mac_s`, as `token_wait_s` is read (320-340).
- `scripts/buildlog/index.py`: `calls` gains `mac TEXT`, `mac_reason TEXT`, `mac_s REAL` (`CALL_COLUMNS`, 115-154); records without them read as null (`pick()`, 390-395, gives every missing field as null); `SCHEMA_VERSION` 9 → 10 (33). `scripts/buildlog/test_index.py:463-469` asserts the version.
- An **offloaded step** is a step whose `call_id` joins a call recorded on another host. `known_crate_steps` (378-384), `kinds()` (231), `summary()` (896), the host count in `report()` (953) and the waiting, kind, test-builds and tests-per-edit sections leave offloaded steps out: they describe natedev. One SQL predicate, defined once, serves every one of them.
- `scripts/buildlog/report.py`: new `mac_tests_section(connection, day)`, after `test_builds_section` in `report()` (951-975), omitted when no call that day has `mac` set. Heading `Tests on the Mac`. One row per package: `Package | Tried | Failed on the Mac | Passed there | Mac p50 | natedev p50 | Stayed local`, where `Tried` counts calls with `mac` of `passed`, `passed_filter`, `failed` or `lost`, `Passed there` counts `passed` and `passed_filter`, `Mac p50` is the median `mac_s` of those and `failed`, `natedev p50` is the median `wall_s` of that day's test calls for the package that have a `nextest` step of their own on natedev's host (a call that a recorded pass answered ran nothing and is left out, whatever its `mac` word); `wall_s` starts after the cargo token is taken, so it never holds the Mac attempt and nothing is subtracted from it, and `Stayed local` lists each decline reason with its count in plain words (`blocked 3, Mac busy 2`). One `Source:` line follows the table. Then two lines:
  - `natedev runs avoided: <N>` — calls with `mac` of `failed` or `passed_filter`, the two that ended on the Mac, less each `failed` call the `--local` line below matches, which natedev ran after all — `about <X> min of build and test time and <Y> min of waiting, estimated from that day's natedev runs of the same packages` (per package, its count times its `natedev p50`, and its count times the median `token_wait_s` of the same calls). `wait_s` runs from script start to the natedev run's start, so it holds the Mac attempt and the token wait: never add it to a Mac total. The token-holder matching (`report.py` 284-293) stays right, because the token wait still ends where the run begins.
- The three columns are read into one value where the rows leave SQL: `NoOffloadRecord | OffloadDeclined(reason, seconds) | MacRunCompleted(outcome, seconds) | MacRunLost(seconds)`, the outcome being `passed`, `passed_filter` or `failed`. A decline never reached the Mac, so no name calls it an attempt. The section's code never passes three loose optional values around.
  - `Failed on the Mac, then passed on natedev with --local: <N> (<packages>)` — a `failed` call followed within 30 minutes, same worktree and package, by a call with reason `local_flag` and status 0 that has a `nextest` step of its own on natedev. Omitted at zero.

**Files:**
- `scripts/buildlog/record.py` — the three call fields.
- `scripts/buildlog/index.py` — columns, defaults, schema version.
- `scripts/buildlog/report.py` — the section and the offloaded-step filter.
- `scripts/buildlog/test_record.py`, `scripts/buildlog/test_index.py`, `scripts/buildlog/test_report.py` — tests.

**Seats:** `1 writer + 1 tester` — record, index and report are one chain; the tests come from this Spec.
- `impl` — `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `scripts/buildlog/report.py`
- `test` — the three test files: the fields round-trip, old records read as null, the schema version, the section's rows and both lines from a fixture with a second host folder `Mac` (the fixture at `test_report.py:50-57`), the section's absence with no Mac calls, the runs-avoided count with a failed and a `passed_filter` call, the avoided minutes from a fixture with known `wall_s` and `token_wait_s` for two packages, a `--local` call that a recorded pass answered (no step) staying out of `natedev p50` and out of the `--local` line, a failure natedev then passed staying out of runs avoided, and an offloaded Mac step staying out of each of Waiting, every kind section, Test builds, Tests per edit, Rebuilds, the Summary counts and the host count, with one assertion per query path.

**Constraints from prior phases:** `verify.sh` already passes `BUILDLOG_MAC` (`passed` for a Mac pass that natedev then confirmed in the same call, `passed_filter` for a filtered Mac pass that ended there, `failed`, `lost`, `declined`), `BUILDLOG_MAC_REASON` (`off`, `repo`, `linux_only`, `backoff`, `blocked`, `busy`, `state`, `unreachable`, `mac_busy`, `battery`, `disk`, `copy`, `local_flag`, or empty) and `BUILDLOG_MAC_S` to `record.py call`. The Mac writes the nextest step itself, with natedev's call id, host `Mac`, and no repo, worktree or sha; it reaches natedev's index with the hourly sync. `verify.sh` starts `wall_s` after the cargo token is taken and calls the Mac runner before the token, so `wall_s` is natedev's run alone and `wait_s` holds the Mac attempt.

**Acceptance gate:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` ends `OK`; `basedpyright scripts/buildlog` ends `0 errors, 0 warnings, 0 notes`.

### Phase 8 — Live on the Mac, then switched on  · status: todo

#### Work Order

**Blocked by:** the showrunner's word that the Ian Hubert demo on the Mac is over (natedev, 2026-10-07). Until then nothing of this feature writes to or runs on the Mac; reached earlier, the run holds here.

**Goal:** A real hana test run goes to the Mac and comes back right in every case, the limits are set from measured numbers, and the feature is switched on.

**Spec:**
This is the first phase that writes to or runs on the Mac. Work in a scratch clone of hana (`git clone --local --branch init/catalyst /home/natepiano/rust/hana <scratch>`), never in another unit's worktree, with `MAC_TEST_CONFIG` pointing at a scratch copy of the config with `offload=on`. Keep saved output small and delete the clone at the end. Run the checks in order, fix each defect where it lives with a regression test, and record every number in the report back:
1. Each probe line parses on the Mac; a block makes the call stay local.
2. The first and second tree copy: times, and that `target`, `.git` and `.direnv` are absent from the mirror and the Mac's rsync accepts the options.
3. `verify.sh test hana --filter <one test>`: the Mac's cold build time, then its rebuild time after touching `crates/hana/src/tool/surface.rs`, against natedev's.
4. Exit status passes through for a pass, a failing test and a compile error.
5. An interrupt mid-run leaves no cargo, rustc or test process on the Mac and frees the claim.
6. A block placed mid-run is pending, the run finishes, exactly one "Mac is free" message arrives, and the next call stays local. This check flips hana's real `MACOS_CI`, so tell the showrunner before it and keep it to a few minutes: record the variable's value first, and if it is not `true`, stop before placing the block and tell the showrunner: the phase ends with the switch on, and it never turns on a switch someone else turned off; while the block is in place, the real `zsh ~/.claude/scripts/production/mac_run.sh <scratch> <its HEAD> 60` exits 9 before any bundle, copy or build, and `gh variable get MACOS_CI --repo natepiano/hana` prints `false`; after `unblock` it prints the recorded value again, and the skipped list `unblock` printed goes into the report back. Whatever fails in between, put the recorded value back before going on. Also settle the block's CI busy rule from a real run, read-only: for the next hana CI run that starts, list its jobs every second from its start (`gh run view <id> --repo natepiano/hana --json jobs`) and record whether `macOS: Compile and Test` is listed before `macOS: Runner Availability` ends; and for a run started while the switch is off, whether the Mac job is listed as `skipped` at once. If the Mac job appears only after the availability job ends and a skipped one is listed at once, replace the rule in `ci_activity` with the simpler one: a queued or in-progress run of `ci_workflow` is busy until a job named `ci_job` is listed with status `completed`; remove `ci_gate_job` from `config/mac_test.conf`, `config/README.md` and the tests, and add tests for an active run that lists no Mac job (busy) and one that lists it skipped (idle). Otherwise keep `ci_gate_job` and record why.
7. After `buildlog sync`, the Mac's step is in natedev's index with the call id, and the report's `Tests on the Mac` section shows the run.
8. The full `hana` and `hana_diegetic` suites on the Mac on a green tree: each test that fails only there goes into `mac_skip.hana`; a package with many goes onto `linux_only.hana`.
9. The mirror's build-folder size after both suites sets `mac_budget_gib.hana`; `free_floor_gib` is set so the Mac keeps room (it had 187 GiB free).
10. One call from inside a Codex seat's shell reaches the Mac.
Then set `offload=on` in `config/mac_test.conf` with the measured limits.

**Files:**
- `config/mac_test.conf` — `offload=on` and the measured limits.
- `scripts/mac_test/offload.py` — defects the live run finds.
- `scripts/mac_test/test_offload.py` — a regression test for each.
- `scripts/mac_test/mac_test.py`, `scripts/mac_test/test_mac_test.py`, `config/README.md` — the CI busy rule, only if check 6 settles it.
- `scripts/delegate/verify.sh`, `scripts/production/mac_run.sh`, `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `scripts/buildlog/report.py` — only a defect a live check finds there, each with a regression test in `scripts/delegate/test_verify_mac_offload.py`, `scripts/production/test_mac_run.py` or the matching `scripts/buildlog/test_*.py`.

**Seats:** `1 writer + 1 tester` — the checks are serial against one Mac, so one writer holds the run; the tester writes stand-in regression tests on natedev and never contacts the Mac.
- `impl` — `config/mac_test.conf`, `config/README.md`, `scripts/mac_test/offload.py`, `scripts/mac_test/mac_test.py`, `scripts/delegate/verify.sh`, `scripts/production/mac_run.sh`, `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `scripts/buildlog/report.py`, every live check and the report back
- `test` — `scripts/mac_test/test_offload.py`, `scripts/mac_test/test_mac_test.py`, `scripts/delegate/test_verify_mac_offload.py`, `scripts/production/test_mac_run.py`, `scripts/buildlog/test_record.py`, `scripts/buildlog/test_index.py`, `scripts/buildlog/test_report.py`: a regression test for each defect `impl` reports

**Constraints from prior phases:** `verify.sh test` calls `scripts/mac_test/offload.py run` before the cargo token; `--local` skips it. `mac_test.py` has `status`, `block`, `unblock`; a block switches hana's `MACOS_CI` variable to `false` through `gh` and `unblock` switches it back. A block stays pending while a queued or in-progress run of `ci_workflow` lists a job named `ci_job` or `ci_gate_job` (`macOS: Runner Availability`, which reads the switch before the Mac job is created) whose status is not `completed`; for about a second between the availability job ending and the Mac job being listed, that check can read idle. A missing or empty `ci_repo` turns every `gh` call off. The report has a `Tests on the Mac` section. The mirror is `~/.local/state/mac-test/mirror/hana` on the Mac. `scripts/production/mac_run.sh` claims the same lock and exits 9 when the Mac is blocked, busy with a test, or its state cannot be read. The showrunner footer and the dailies read the block file, so a test that renders either sets `MAC_TEST_STATE_DIR`.

**Acceptance gate:** every check above has a recorded result; `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` ends `OK`; `basedpyright scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`; for each other directory a fix touched, its own tests and type check pass as that directory's phase gate states them (`scripts/delegate` `test_verify_*.py`, `scripts/production` `test_mac_run.py`, `scripts/buildlog` `test_*.py`); `python3 ~/.claude/scripts/mac_test/mac_test.py status` prints `free`, `no block` and `CI's Mac switch: on` at the end, and no scratch clone remains.
