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

#### Work Order

**Goal:** A block on the Mac also keeps CI's Mac job from starting, lifts by itself when its holder forgets it, and says on the way out which commits went without a macOS check.

**Spec:**
The hana workflow starts its one macOS job, `macOS: Compile and Test`, only while the repository variable `MACOS_CI` is `true` (`/home/natepiano/rust/hana/.github/workflows/ci.yml:511`). The same line also needs `HANA_LINUX_CI` not `false`, and the job needs the Mac runner online (604). So `MACOS_CI` is a switch that can only take the Mac away from CI: with any other value the job is skipped and the run stays green, as when the Mac is away. The block uses that switch, and every line this command prints speaks of the switch (`CI may use the Mac`, `CI's Mac switch`), never of the job being on. Everything here calls `gh` (found on `PATH`) from natedev; nothing touches the Mac.

New file `config/mac_test.conf`, each key commented in plain words, read with `config_values()` imported from `scripts/lint/sweep.py` (the import form of `scripts/buildlog/disk.py:15-16`). `MAC_TEST_CONFIG` overrides the path; the default is `~/.claude/config/mac_test.conf`. Every test sets `MAC_TEST_CONFIG` to a scratch file, the first phase's tests included, so no test reads the live config:
```
ci_repo=natepiano/hana
ci_variable=MACOS_CI
ci_workflow=ci.yml
ci_job=macOS: Compile and Test
gh_timeout_s=30
block_hours=4
block_max_hours=48
block_warn_minutes=15
```
An empty or missing `ci_repo` turns the CI switch, the busy check and the skipped list off; expiry still applies.

**Two locks.** `state.lock` guards the files and is held only for a read or a write, never across a `gh` call, a message send or a process start. A second flock, `control.lock`, is held by `block`, `unblock`, `status` and each pass of `watch` from their first read to their last write, slow calls included, so those four never interleave; they take `control.lock` first and `state.lock` inside it. `claim` and `release` take only `state.lock`, never call `gh` and never send a message: `claim` removes a dead run and answers 10 on any block; `release` removes its own run and then, when a block is present and no watcher holds `watch.lock`, starts the watcher. Only a holder of `control.lock` writes `block.json`, and a new block is written there before the command's first `gh` call, so a `claim` made meanwhile answers 10. This replaces the first phase's `settle()`: a pending block turns active, and the "Mac is free" message goes out, only in the full settle below.

**The block record.** `block.json` becomes
`{"version": 2, "holder": str, "for": str, "since": "<ISO-8601 UTC>", "expires": "<ISO-8601 UTC>", "session": str | null, "showrunner": str | null, "state": "pending" | "active", "waiting_on": {"kind": "local_run", "what": str} | {"kind": "ci_job"} | {"kind": "ci_unknown"}, "free_message": {"kind": "not_needed"} | {"kind": "owed", "what": str, "ended": "<ISO-8601 UTC>"} | {"kind": "sent"}, "ci": "off_by_this_block" | "off_before" | "not_configured" | "still_on"}`,
with `waiting_on` present only when pending and `free_message` only when active. JSON null and the optional keys stop at the decoder. `read_block(path)` returns `NoMacBlock | PendingMacBlock | ActiveMacBlock` (frozen dataclasses). Both block types carry `holder`, `reason`, `since` and `expires` (timezone-aware `datetime`), `delivery: RecordedSession | HolderQueue` (where the holder's messages go), `showrunner: NamedShowrunner | NoShowrunner` and `ci: CiSwitch` (the four words as a `Literal`). `PendingMacBlock` adds `waiting_on: WaitingOnLocalRun | WaitingOnCiJob | WaitingOnUnknownCi`; `ActiveMacBlock` adds `free_message: FreeMessageNotNeeded | FreeMessageOwed | FreeMessageSent`. The decoder raises `ValueError` on malformed JSON, a missing or mistyped key, a word outside its set, or a timestamp that is not timezone-aware ISO-8601. A record with no `version` key was written by the command as it is today, and one may be live when this lands: it decodes with `expires` = `since` + 48 hours, no showrunner, `ci` `still_on`, waiting on a local run named `a test` when pending, and no message needed when active. The next full settle then switches CI off for it like any block whose switch call has not succeeded, and writes it back in the new form. `read_block` stays a plain read with no lock, no settle, no config and no message, and `state_paths()` stays importable: the footer imports both in the next phase. The run record keeps its shape.

**Calling `gh`.** One helper runs every `gh` call under `gh_timeout_s` and returns `GhAnswer(stdout)` or `GhFailure(line)`, where `line` is the last non-empty line `gh` printed, or `gh timed out after <n> s`. Nothing retries inside a command; the watcher's next pass is the retry.
- **The CI busy check** answers `CiIdle | CiBusy | CiUnknown(line)`. CI is busy when some run of `ci_workflow` with status `queued` or `in_progress` has a job named `ci_job` whose status is `queued` or `in_progress`. Read it with `gh run list --repo <ci_repo> --workflow <ci_workflow> --status <status> --json databaseId` for both statuses (a run reads `queued` while its Mac job is `in_progress`), then `gh run view <id> --repo <ci_repo> --json jobs` for each id. Any failing call makes the answer `CiUnknown`.
- **Switching CI off:** `gh variable get <ci_variable> --repo <ci_repo>`; when it prints `true`, `gh variable set <ci_variable> --body false --repo <ci_repo>` and the block's `ci` becomes `off_by_this_block`; any other value, `off_before`; a failing call leaves `still_on`.
- **Switching CI back**, only for `off_by_this_block`: `gh variable set <ci_variable> --body true --repo <ci_repo>`.

**The full settle**, run under `control.lock` by `block`, `status` and each pass of `watch`, in this order:
1. Remove a dead run.
2. A block whose `expires` has passed lifts (Expiry, below) and the settle ends.
3. `ci` is `still_on`: with an empty `ci_repo` it becomes `not_configured`; otherwise switch CI off.
4. A pending block: with a live run it waits on that run (`what` from the run record). Otherwise, with `ci_repo` set, the busy check decides: `CiBusy` → waiting on CI's job, `CiUnknown` → waiting on unknown CI. Otherwise it turns active with the message owed: `what` is the run's `what` when it last waited on a local run and `CI's Mac job` when it last waited on CI, `ended` is now.
5. An active block with the message owed: send the "Mac is free" message (the first phase's text and delivery rule, with `--repeat-minutes 1440` added beside its `--key`, so a repeat is skipped). send.py exit 0 or 1 records it sent; any other exit leaves it owed for the next pass.
6. Inside `block_warn_minutes` of `expires`: the warning message (Expiry, below).
7. A block remains and no watcher holds `watch.lock`: start the watcher.

Commands:
- `block --holder NAME --for TEXT [--hours N] [--showrunner NAME]`: N defaults to `block_hours` and must be a finite number with `0 < N <= block_max_hours`; otherwise print `a block lasts more than 0 and at most <block_max_hours> hours` and exit 2 with nothing written. Another holder's block: `already blocked by <holder>`, exit 1. A new block is written at once with `ci` `still_on`: pending on the live run, else pending on unknown CI when `ci_repo` is set, else active with no message needed. Then the full settle runs; a block that turns active inside the command that made it needs no message, because that command's own output says so. The same holder again renews: reason, `expires`, showrunner and session become this call's; `since`, the state and `ci` are kept; then the full settle runs. The first output line follows the resulting state: active → `Mac blocked for <holder>: nothing is running there, it is free now.`; waiting on a local run → the first phase's pending line; waiting on CI's job → `Block pending for <holder>: CI's Mac job is running. Nothing new starts there, and you get a message when it ends.`; waiting on unknown CI → `Block pending for <holder>: CI could not be checked (<line>). Nothing new starts there, and you get a message once CI's Mac job is known to be idle.` When `ci` is still `still_on`, add `CI can still use the Mac: <line>. Run github-warmup, then block again.` Every `block` ends with `It lifts by itself at <day HH:MM zone> unless you run block again.` (`%a %H:%M %Z`, local zone). Exit 2 when `ci` is `still_on`, else 0.
- `unblock --holder NAME` (under `control.lock`, no full settle): no block → `no block`, exit 0; another holder's → `blocked by <holder>`, exit 1. Otherwise switch CI back when `ci` is `off_by_this_block`; when that call fails the block stays: `Mac still blocked: CI's Mac switch could not be turned back on (<line>). Run github-warmup, then unblock again.`, exit 2. Then remove the block and print, by its `ci`: `off_by_this_block` → `Mac unblocked; CI may use the Mac again.`; `off_before` → `Mac unblocked; CI's Mac switch was already off and stays off.`; `still_on` or `not_configured` → `Mac unblocked.` Then the skipped list, unless `ci` is `not_configured`.
- **Expiry.** In a full settle, a block whose `expires` has passed lifts: CI is switched back as in `unblock`, `block.json` is removed, and one message goes to the holder (the first phase's delivery rule) and, when the block names one, to the showrunner by name: `Message from mac-test: the Mac block by <holder> (<for>) reached its time limit at <day HH:MM zone> and lifted by itself.`, then ` CI may use the Mac again.` when this block had switched CI off, then the skipped list. When CI cannot be switched back the block stays, the next settle tries again, and the holder and the showrunner get `Message from mac-test: the Mac block by <holder> is past its time limit but still in place: CI's Mac switch could not be turned back on (<line>). Run github-warmup, then unblock.` with `--key mac-block-stuck-<since> --repeat-minutes 60`. `block_warn_minutes` before `expires`, one message to the holder: `Message from mac-test: your Mac block (<for>) lifts by itself at <day HH:MM zone>. Run block again to keep it.`, sent with `--key mac-block-warn-<expires> --repeat-minutes 1440`, so it goes once per expiry time.
- `watch`: take `watch.lock` without waiting; when another watcher holds it, exit 0 at once. Then every 60 s (`MAC_TEST_WATCH_INTERVAL_S` for tests) run a full settle; exit 0 when no block is left. It is started as its own user service, so it outlives the command and any sandbox that started it, in the form of `scripts/message/notifier.sh:415-419`: `systemd-run --user --collect --quiet --no-block --unit mac-test-watch-<starter's pid> -- /usr/bin/env HOME=<HOME> PATH=<PATH> <each MAC_TEST_* variable that is set> <python> <this file> watch`. A start that fails prints `warning: the Mac block watcher could not be started` on stderr and changes no exit code.
- **The skipped list**, built after CI is switched back and outside `state.lock`: runs of `ci_workflow` created at or after the block's `since` (`gh run list --repo <ci_repo> --workflow <ci_workflow> --created '>=<since>' --limit 200 --json databaseId,headBranch,headSha`, newest first), keeping each whose `ci_job` job has conclusion `skipped` (`gh run view <id> --repo <ci_repo> --json jobs`). It prints
  ```
  CI runs that skipped the macOS job under this block:
    <branch>: <n> runs, newest <first 9 characters of the sha>
  Run CI again on each branch's newest commit to make up its macOS check: gh workflow run <ci_workflow> --repo <ci_repo> --ref <branch>
  ```
  with one branch line per branch, sorted by name, or the single line `No CI run skipped the macOS job under this block.` When the list call returns exactly 200 runs, add `Only the newest 200 runs were looked at; older ones under this block are not listed.` A failing `gh` here prints `Could not list the CI runs that skipped the macOS job: <line>` and changes no exit code.
- `status` (after a full settle; always exit 0): the run line as before. The block line: `no block`, `blocked by <holder> since <day HH:MM zone>, lifts <day HH:MM zone>: <for>`, or `block pending for <holder> (<waiting>), lifts <day HH:MM zone>: <for>` where `<waiting>` is `a test is running`, `CI's Mac job is running` or `CI could not be checked`. Under an active block whose message is owed, one more line: `The "Mac is free" message has not reached <holder> yet.` Then one line for the switch. With a block, from its `ci`: `CI's Mac switch: off (this block)`, `CI's Mac switch: off (set elsewhere)`, `CI's Mac switch: still on (<line>)` or `CI's Mac switch: not configured`. With no block, from one `gh variable get`: `CI's Mac switch: on`, `CI's Mac switch: off (set elsewhere)` or `CI's Mac switch: unknown (<line>)`, and `not configured` with an empty `ci_repo` and no call.

`commands/mac_test.md`: `block <why> [hours N]`; a unit director adds `--showrunner <its showrunner's session name>`. It says what a block does to CI (new CI runs skip the Mac job and stay green, a Mac job already running finishes first, runs started under a block get no macOS check), that a block lifts by itself and how to keep it, that `unblock` lists the commits to run CI on again, and that these calls run with the sandbox off: they write under `~/.local/state`, call `gh` and start a user service. `commands/showrunner/produce.md`, the promote rule (529-534): a skipped `macOS: Compile and Test` also passes when the Mac is blocked; the log line reads `macOS skipped (Mac blocked)`. `config/README.md` gains a `## mac_test.conf` entry. `pyrightconfig.json` gains `{"root": "scripts/mac_test", "extraPaths": ["scripts/mac_test", "scripts/lint"]}`.

**Files:**
- `scripts/mac_test/mac_test.py` — the two locks, the block types, the CI switch, the busy check, expiry, the skipped list, `watch`.
- `scripts/mac_test/test_mac_test.py` — tests for them.
- `config/mac_test.conf` — new, with the keys above.
- `config/README.md` — the new entry.
- `pyrightconfig.json` — the new environment entry.
- `commands/mac_test.md` — hours, CI, expiry, the skipped list.
- `commands/showrunner/produce.md` — one clause in the promote rule.

**Seats:** `1 writer + 1 tester` — the command, its config and docs are one writer's; the tests come from this Spec.
- `impl` — `scripts/mac_test/mac_test.py`, `config/mac_test.conf`, `config/README.md`, `commands/mac_test.md`, `commands/showrunner/produce.md`; hub: `pyrightconfig.json`
- `test` — `scripts/mac_test/test_mac_test.py`: a stand-in `gh` on `PATH` that logs its argv and answers from a fixture file (the variable's value, the run lists, each run's jobs, a failure knob, a delay knob), a stand-in `systemd-run` on `PATH` that logs its argv and, on a knob, starts the command after `--` detached, and the first phase's stand-in `send` and `sessions` files.

**Constraints from prior phases:** `scripts/mac_test/mac_test.py` has `claim`, `release`, `block`, `unblock`, `status` and one `settle(paths, ended_work="Mac work")` that every command runs under `state.lock`: it removes a dead run, turns a pending block active and sends the "Mac is free" message while the lock is held (`send_mac_free`, `notification_target`, `message_script`; `MAC_TEST_SEND` and `MAC_TEST_SESSIONS` name the two scripts). `release` removes its run and settles again. A repeat `block` by the same holder already keeps `since` and takes the caller's session and reason. The records are `TypedDict`s (`RunRecord`, `BlockRecord`) beside `NoRun` and `NoBlock`; this phase replaces the block ones with the types above. `process_start_time` reads `/proc/<pid>/stat` with `errors="replace"`. `scripts/mac_test/test_mac_test.py` drives the CLI as a subprocess, pins the first phase's output lines, and has `start_child(name: bytes = b"mac ) test")` for a stand-in run; its shared `setUp` must set `MAC_TEST_CONFIG`. Its tests that pin a message sent by `release`, or by the settle inside `claim`, change with this phase: the watcher or the next `status` sends it.

**Acceptance gate:** `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` ends `OK`; `basedpyright scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`. Tests prove: `block` records what it found and sets the variable to `false`; `unblock` switches it back only for `off_by_this_block`, and keeps the block with exit 2 when `gh` fails; a block placed while CI is busy is pending, and the watcher turns it active and sends exactly one message once CI is idle; a failing busy check leaves it pending on unknown CI; a block past its `expires` lifts at the next settle, switches CI back and messages the holder and the named showrunner; a switch that cannot be set back at expiry keeps the block; the warning goes once; a renewal keeps `since`, moves `expires` and takes the new session; `--hours` of 0, a negative number, `nan`, `inf` and a value above the maximum each exit 2 with nothing written; the skipped list's three forms and the 200-run line; a failing `gh` on `block` exits 2 with the block written and `ci` `still_on`, and the next settle with a working `gh` switches CI off; a `gh` that hangs is cut off at `gh_timeout_s`; `claim` and `release` make no `gh` call and send nothing; a `claim` made while `block` waits on a slow `gh` exits 10; a renewal made while the watcher waits on a slow `gh` is not overwritten; `unblock` and an expiry at the same moment switch CI back once and remove the block once; a second `watch` exits 0 at once; a `release` under a pending block starts the watcher through `systemd-run`; a message send that exits 3 leaves the message owed, `status` says so, and the next pass sends it; each `status` line, the three with no block included; an empty `ci_repo` makes no `gh` call at all; a record with no `version` decodes, has CI switched off at the next settle and keeps its `since`; malformed JSON, a missing key and a timestamp with no zone each raise `ValueError` from `read_block`; a slow stand-in `send` does not delay a `claim` made meanwhile.

### Phase 3 — A Mac block shows in the footer and the dailies  · status: todo

#### Work Order

**Goal:** A pending or active Mac block shows in every showrunner reply footer and dailies report, beside the build holds, so a block that turns off the macOS check is never out of sight.

**Spec:**
One renderer serves every place a build hold shows. `footer()` in `scripts/production/dailies_render.py:1213-1235` builds the footer for the dailies report (`render`, 1373), for `update_registration.py footer` (through `footer_main`, 1400-1412) and for the Stop hook (`render_footer` in `scripts/hooks/showrunner_footer.py:193-210` runs `dailies_render.py --footer`, and 278-315 compares the result line for line). A build hold's line comes from `hold_line` (1088-1092). The Mac block joins it there:

- Read the block without side effects: add `scripts/mac_test` to `sys.path` beside the `build_hold` insert (57), `from mac_test import ActiveMacBlock, NoMacBlock, PendingMacBlock, read_block, state_paths`, and add `read_mac_block() -> NoMacBlock | PendingMacBlock | ActiveMacBlock | UnreadableMacBlock`, which calls `read_block(state_paths().block)` and turns a `ValueError` or `OSError` into the new `UnreadableMacBlock(path)`. Never run `mac_test.py status` here: it takes the locks, settles, calls `gh` and can send a message, and the Stop hook gives the whole render 10 s.
- New `mac_block_line(block: PendingMacBlock | ActiveMacBlock | UnreadableMacBlock, zone: ZoneInfo) -> str`, times in the footer's `zone` as `hold_line` does them:
  - active: `Mac block: <holder> since <HH:MM zone>, for <for> - lifts <day HH:MM zone>`
  - pending: `Mac block pending: <holder> since <HH:MM zone>, for <for> - lifts <day HH:MM zone>`
  - a block of either state whose `ci` is `still_on` ends ` - CI can still use the Mac`: its holder believes the macOS check is off, and it is not;
  - unreadable: `Mac block: its state file cannot be read (<path>)`.
  (`%H:%M %Z` and `%a %H:%M %Z`.)
- In `footer()` the Mac block item follows the build-hold items and their nested release lines and comes before the agent lines. The schedule line stays the last bullet: the Stop hook's `stamp()` reads it there.
- `footer()` takes that four-way block state as a parameter; `footer_main` and `parse_report` (879) each call `read_mac_block()` once, and `Report` carries the value beside `build_hold` (227). `NoMacBlock` adds no item.
- The block's reason passes `check_plumbing` like a build-hold purpose (`read_dailies_hold`, 689-698); a match raises `InputError` ending `; have <holder> run /mac_test block again with other words`.
- An unreadable block file never refuses the footer: `read_block` raises `ValueError` for malformed JSON, a missing key, a word outside its set or a timestamp with no zone, and the item then reads the unreadable line above.
- No unit row mark and no input key: `parse_report` and `parse_unit` reject unknown keys, and a Mac block holds no unit. `update_registration.py`, `dailies_input.py` and both hook files need no change.

Tests point every run at a scratch state directory with `MAC_TEST_STATE_DIR`, so a block live on this machine never leaks into a test:
- `scripts/production/test_dailies_render_holds.py` — add `MAC_TEST_STATE_DIR` to `run_script`'s env (86-99) and new cases: the active line, the pending line, the `still_on` ending on each, the order (build hold, Mac block, agents, schedule last), no block and no line, a plumbing word refused with the hint, and the unreadable line for malformed JSON, for a missing key and for a timestamp with no zone. Block files in tests are written in the `version` 2 form the constraint below gives.
- Set `MAC_TEST_STATE_DIR` in `test_dailies_render.py` (`run()` 82-97 and `StatePreflightTests.setUp` 769-772), `test_dailies_render_agents.py` (`setUp` 34-59), `test_dailies_input.py` (the env at 126), `test_update_registration.py` (the env at 74-77; `copied_command` at 119-130 also copies `scripts/mac_test`) and `scripts/hooks/test_stop_showrunner_footer.py` (`setUp` 90-117).

`pyrightconfig.json`: the `scripts/production` entry (75-82) gains `scripts/mac_test` in `extraPaths`. Docs: `commands/showrunner/produce.md` — the hold sentence (126-127) also names the Mac block and its state directory, and the example footer (144-160) gains a Mac block line; `commands/showrunner/dailies.md` — 171, 249 and 250-252 name the Mac block line; the `dailies_render.py` docstring (35-36).

**Files:**
- `scripts/production/dailies_render.py` — read the block, `mac_block_line`, the footer item.
- `scripts/production/test_dailies_render_holds.py` — the new cases.
- `scripts/production/test_dailies_render.py`, `scripts/production/test_dailies_render_agents.py`, `scripts/production/test_dailies_input.py`, `scripts/production/test_update_registration.py`, `scripts/hooks/test_stop_showrunner_footer.py` — the scratch state directory.
- `pyrightconfig.json` — one path.
- `commands/showrunner/produce.md`, `commands/showrunner/dailies.md` — the Mac block line.

**Seats:** `1 writer + 1 tester` — the renderer and its docs are one writer's; the tests come from this Spec.
- `impl` — `scripts/production/dailies_render.py`, `commands/showrunner/produce.md`, `commands/showrunner/dailies.md`; hub: `pyrightconfig.json`
- `test` — `scripts/production/test_dailies_render_holds.py`, `scripts/production/test_dailies_render.py`, `scripts/production/test_dailies_render_agents.py`, `scripts/production/test_dailies_input.py`, `scripts/production/test_update_registration.py`, `scripts/hooks/test_stop_showrunner_footer.py`

**Constraints from prior phases:** `scripts/mac_test/mac_test.py` exports `state_paths()` (its `.block` is the block file; `MAC_TEST_STATE_DIR` moves the directory) and a plain `read_block(path) -> NoMacBlock | PendingMacBlock | ActiveMacBlock`: no lock, no settle, no config, no message. Both block types are frozen dataclasses with `holder`, `reason`, `since` and `expires` (timezone-aware `datetime`) and `ci`, one of `off_by_this_block`, `off_before`, `not_configured`, `still_on`. It raises `ValueError` on malformed JSON, a missing key, a word outside its set or a timestamp with no zone. On disk a block is `{"version": 2, "holder", "for", "since", "expires", "session", "showrunner", "state": "pending" | "active", "waiting_on" (pending) or "free_message" (active), "ci"}`; copy a real one from `scripts/mac_test/test_mac_test.py` for the fixtures.

**Acceptance gate:** `python3 -m unittest discover -s scripts/production -p 'test_dailies_*.py'` ends `OK`; `python3 -m unittest discover -s scripts/production -p 'test_update_registration.py'` ends `OK`; `python3 -m unittest discover -s scripts/hooks -p 'test_stop_showrunner_footer.py'` ends `OK`; `basedpyright scripts/production` ends `0 errors, 0 warnings, 0 notes`.

### Phase 4 — The offload runner: probe, copy the tree, run on the Mac  · status: todo

#### Work Order

**Goal:** One command takes a nextest argv and either runs it on the Mac against a copy of the worktree, streaming the output back, or says in one word why the run stays local.

**Spec:**
New `scripts/mac_test/offload.py`:
`offload.py run --repo-root DIR --package PKG --call-id ID [--filter-run] [--result FILE] -- <words after "cargo nextest run">`

Exit codes: `0` passed on the Mac; `75` not run there (declined or lost; the run must happen locally); any other value is the Mac run's own exit status. With `--result`, write three lines: `mac=<passed|failed|lost|declined>`, `reason=<word or empty>`, `seconds=<float>`. A decline prints one line `mac_test: staying on natedev (<reason>)`. Those three lines are a wire form for `verify.sh` only. Inside `offload.py` the outcome is one value, `MacOffloadResult = PassedOnMac(seconds) | FailedOnMac(status, seconds) | LostMacRun(seconds) | DeclinedMacRun(reason: MacDeclineReason, seconds)`, with `MacDeclineReason` a `Literal` of the decline words below; the exit code and the result file are both written from it by one function, `write_result()`, so no reason can travel with a pass and no status with a decline.

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
A repository is offloadable only when a `linux_only.<repo>` key exists, empty or not. `<repo>` is the name of the directory holding the git common dir (`git -C <root> rev-parse --git-common-dir`, as `repo_name()` in `sweep.py`).

Steps, in order; each failure is a decline with the reason shown:
1. `offload` is not `on` → `off`. No `linux_only.<repo>` key → `repo`. PKG in that list → `linux_only`.
2. `unreachable_until` in the state directory is in the future → `backoff`.
3. `mac_test.py claim --pid <own pid> --what "nextest <PKG>" --worktree <root>`: exit 10 → `blocked`, 11 → `busy`. From here on, always run `mac_test.py release --pid <own pid>` before exiting, on signals too.
4. Probe, one ssh call: `ssh -o BatchMode=yes -o ConnectTimeout=<connect_timeout_s> <host> '<script>'` under `probe_timeout_s`. The script makes the mirror directory `.local/state/mac-test/mirror/<repo>` and prints `procs=<count of cargo, rustc and cargo-nextest processes of any user>`, `load=<1-minute load>`, `power=<ac|battery>`, `free_gib=<free GiB on the home volume>`, `host=<hostname -s>`, `rc=0`, using `pgrep -x`, `sysctl -n vm.loadavg`, `pmset -g batt` and `df -g "$HOME"`. ssh exit 255, a timeout or no `rc=0` line → write `unreachable_until = now + unreachable_backoff_s`, reason `unreachable`. `procs` above 0 or `load` above `max_load` → `mac_busy`. `power=battery` → `battery`. `free_gib` below `free_floor_gib` → `disk`.
5. Copy: write an exclude file holding `/.git`, `/target/` and one anchored line per path from `git -C <root> ls-files --others --ignored --exclude-standard --directory`. Then `rsync -rlpc --delete --exclude-from=<file> -e "ssh -o BatchMode=yes -o ConnectTimeout=<n>" <root>/ <host>:.local/state/mac-test/mirror/<repo>/`. No `-t` and no `-a`: a file whose content is unchanged keeps its Mac modification time, so cargo rebuilds only what changed, whichever worktree the copy came from. A nonzero rsync status → `copy`.
6. Run: print `mac_test: running on the Mac (macOS, host <host>); tree copied in <s> s`. Then one ssh call whose remote command is, built with `shlex.join` and `shlex.quote`:
   `cd <mirror> && PATH="$HOME/.cargo/bin:$PATH" BUILDLOG_CALLER=verify BUILDLOG_CALL_ID=<ID> BUILDLOG_SYNC=1 LINT_SWEEP_BUDGET_GIB=<mac_budget_gib.<repo>, else mac_budget_gib> ~/.claude/scripts/lint/lint nextest <words>; echo mac_exit=$?`
   When `mac_skip.<repo>` is not empty, the value after `-E` becomes `(<given>) & not (<mac_skip>)`. Stream the remote stdout and stderr line by line as they arrive; hold back the last `mac_exit=<n>` line. No such line → `lost`, exit 75. `mac_exit=0` → exit 0. Otherwise print `mac_test: this ran on the Mac (macOS). A failure that looks unrelated to your change may be a macOS difference; rerun with --local to run it on natedev.` and exit `<n>`.
7. On SIGTERM or SIGINT: stop the ssh child, try once for at most 10 s to end what it started (`ssh <host> "pkill -f <mirror>"`), release, and exit 128 plus the signal number.

The `## mac_test.conf` entry in `config/README.md` gains the new keys.

**Files:**
- `scripts/mac_test/offload.py` — new: the runner.
- `scripts/mac_test/test_offload.py` — new: its tests.
- `config/mac_test.conf` — the offload keys.
- `config/README.md` — the entry's new keys.

**Seats:** `1 writer + 1 tester` — the runner and its config are one writer's; the tests come from this Spec.
- `impl` — `scripts/mac_test/offload.py`, `config/mac_test.conf`, `config/README.md`
- `test` — `scripts/mac_test/test_offload.py`: one stand-in script installed as `ssh` and `rsync` on `PATH` (the form in `scripts/production/test_merge_checkpoint.py:44-58`), logging argv, with knobs for each probe line, `mac_exit=<n>`, ssh's own 255, a slow answer and a missing last line.

**Constraints from prior phases:** `scripts/mac_test/mac_test.py` exists with `claim` (0 claimed, 10 blocked, 11 busy) and `release`, state under `MAC_TEST_STATE_DIR`. Both take only the short state lock, never call `gh` and never send a message, so neither can stall a worker; `release` under a pending block starts the block watcher through `systemd-run`, so tests put a stand-in `systemd-run` on `PATH` or leave no block in place. `config/mac_test.conf` exists with the `ci_*`, `gh_timeout_s` and `block_*` keys, read through `MAC_TEST_CONFIG` with `config_values()`; `pyrightconfig.json` already lists `scripts/mac_test` with `scripts/lint` on its path.

**Acceptance gate:** `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` ends `OK`; `basedpyright scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`. Tests prove: each decline reason, with exit 75 and the result file; a second call inside the back-off makes no ssh call; the rsync argv and the exclude file; the remote command carries the call id, the budget and the exact nextest words, with a filter such as `package(hana) & (test(a) | test(b))` surviving quoting; `mac_skip` wraps the filter; output order is kept and the `mac_exit` line is not shown; exit 0, a failing status and a lost connection; the claim is released on every path, a signal included.

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
- An interrupt while the runner is active reaches the runner, which releases its claim.
- No other verb calls the runner; `final` stays on natedev.

`scripts/production/mac_run.sh`: after the reach probe (line 45), `python3 ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ --what "mac run ${sha[1,9]}" --wait 600`. Exit 10 or 11: print the line it gave (`blocked by <holder>: <for>` or `busy: <what> since <HH:MM>`) and exit 9, a new code that means the Mac is blocked or busy with a test. Exit 3 keeps meaning only that the Mac is unreachable, which hana's showrunner reads as an outage (natedev, 2026-10-07). Release on every exit path. Update the header's exit list.

`commands/showrunner/produce.md`, the Mac run rule (503-509): add that exit 9 means the Mac is blocked or busy with a test, so the run is skipped and it is no outage. `commands/unit/delegate.md`, the `<VerificationContract/>` table: add the row `a Mac failure that looks unrelated to the change, run on natedev` → `bash ~/.claude/scripts/delegate/verify.sh test <package> --local`.

**Files:**
- `scripts/delegate/verify.sh` — `--local`, the composed argv, the Mac step, the call record fields.
- `scripts/delegate/test_verify_mac_offload.py` — new.
- `scripts/production/mac_run.sh` — claim and release.
- `scripts/production/test_mac_run.py` — new: the claim, exit 9 and the release.
- `commands/showrunner/produce.md` — one clause in the Mac run rule.
- `commands/unit/delegate.md` — one table row.

**Seats:** `1 writer + 1 tester` — one writer holds the scripts and docs; the tests come from this Spec.
- `impl` — `scripts/delegate/verify.sh`, `scripts/production/mac_run.sh`, `commands/showrunner/produce.md`, `commands/unit/delegate.md`
- `test` — `scripts/delegate/test_verify_mac_offload.py`: the fixture of `test_verify_token_wait.py` (its stand-in `cargo` extended to answer `nextest --version` and `nextest run`), with `VERIFY_MAC_RUNNER` pointing at a stand-in runner that logs its argv, writes the result file and exits as told. `scripts/production/test_mac_run.py`: `mac_run.sh` run with a temp `HOME` whose `.claude` links to the repo root, stand-in `ssh`, `scp` and `git` on `PATH` (the form in `scripts/production/test_merge_checkpoint.py:44-58`), and a stand-in `mac_test.py` that logs its argv and exits as told.

**Constraints from prior phases:** `scripts/mac_test/offload.py run` exits 0 (Mac pass), 75 (run locally) or the Mac run's status, and writes `mac=`, `reason=`, `seconds=` lines to `--result`. `scripts/mac_test/mac_test.py claim --wait` exits 0, 10 (prints `blocked by <holder>: <for>`) or 11 (prints `busy: <what> since <HH:MM>`). `config/mac_test.conf` ships `offload=off`, so nothing reaches the Mac yet.

**Acceptance gate:** `python3 -m unittest discover -s scripts/delegate -p 'test_verify_*.py'` ends `OK` with `test_verify_untested_examples.py` unchanged; `basedpyright scripts/delegate` ends `0 errors, 0 warnings, 0 notes`; `bash -n scripts/delegate/verify.sh` and `zsh -n scripts/production/mac_run.sh` pass; `python3 -m unittest discover -s scripts/production -p 'test_mac_run.py'` ends `OK`. `mac_run.sh` tests prove: a claim answering 10 or 11 prints the line it gave and exits 9 with no build started; an unreachable Mac still exits 3; the claim is released after a pass, after an early failure and after SIGTERM. `verify.sh` tests prove: the runner gets the same words the local run would; exit 75 runs locally with the token as before; a Mac failure exits with that status, runs no local cargo and writes no pass record; a Mac pass on a full run then runs locally and only that run writes the pass record; a Mac pass on a `--filter` run ends there; `--local` never calls the runner and a recorded pass answers both forms; `check`, `lint` and `final` never call it; the call record carries the three fields, with `passed_filter` for a filtered Mac pass.

### Phase 6 — Which packages are pointless on the Mac  · status: todo

#### Work Order

**Goal:** One command lists, for any Rust workspace, the packages whose tests depend on the operating system, so the Linux-only list can be checked.

**Spec:**
New `scripts/mac_test/audit.py <repo dir> [--check]`. It reads the root `Cargo.toml` with `tomllib`, expands `[workspace] members` globs and drops `exclude` entries; it runs no cargo. For each member:
- **gated sites:** lines in `src/`, `tests/` and `build.rs` matching `target_os`, `cfg(unix)`, `cfg(windows)`, `target_family`, counted for source and tests apart;
- **target dependencies:** the keys of the member's `[target.'cfg(…)'.dependencies]` and `dev-dependencies` tables;
- **listed:** whether `linux_only.<repo>` in `MAC_TEST_CONFIG` names it;
- **suggestion**, from every predicate found, in gated sites and target tables alike. Each predicate is one of three kinds: *Linux only*, true on Linux and false on macOS (`target_os = "linux"`, `not(target_os = "macos")`); *same on both* (`unix`, `target_family = "unix"`, `not(windows)`, `windows`) or *macOS only* (`target_os = "macos"`), neither of which keeps a package off the Mac; and *unclassified*, any other form, a compound `any(…)` or `all(…)` included. A member with a Linux-only predicate is `keep on natedev`; else one with an unclassified predicate is `needs review`; else `can go to the Mac`.

Output is a markdown table: `Package | Gated sites (src) | Gated sites (tests) | Target dependencies | Listed | Suggestion`, largest counts first, then one line naming each package whose suggestion and listing disagree, and one naming each `needs review` package with its unclassified predicates. `--check` exits 1 when a `keep on natedev` package is not listed; a `needs review` package is printed and does not fail it, because natedev still confirms every pass. `commands/mac_test.md` gains `audit <repo dir>` with one line on reading the table: gating is at compile time, so a gated test does not fail on the Mac, it is simply not built there.

**Files:**
- `scripts/mac_test/audit.py` — new.
- `scripts/mac_test/test_audit.py` — new.
- `commands/mac_test.md` — the `audit` argument.

**Seats:** `1 writer + 1 tester` — one small command; the tests come from this Spec.
- `impl` — `scripts/mac_test/audit.py`, `commands/mac_test.md`
- `test` — `scripts/mac_test/test_audit.py`: a temp workspace of five members (a linux target table, a Linux-gated test only, a macOS-only dependency, a compound predicate, and one plain), member globs and `exclude`, the table with all three suggestions, both lines under it and `--check`.

**Constraints from prior phases:** `config/mac_test.conf` holds `linux_only.<repo>=<comma list>`; read it with `config_values()` from `scripts/lint/sweep.py`, as `offload.py` does. `commands/mac_test.md` exists with `block`, `unblock`, `status`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` ends `OK`; `basedpyright scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`.

### Phase 7 — The build log and the report show what the Mac took  · status: todo

#### Work Order

**Goal:** The build report says how many test runs the Mac took, how they went, why others stayed local, and how much natedev time and waiting that removed.

**Spec:**
- `scripts/buildlog/record.py call`: read `BUILDLOG_MAC`, `BUILDLOG_MAC_REASON`, `BUILDLOG_MAC_S` into fields `mac`, `mac_reason`, `mac_s`, as `token_wait_s` is read (320-340).
- `scripts/buildlog/index.py`: `calls` gains `mac TEXT`, `mac_reason TEXT`, `mac_s REAL` (107-141); records without them read as null (`pick()`, 390-395, gives every missing field as null); `SCHEMA_VERSION` 9 → 10 (33). `scripts/buildlog/test_index.py:463-469` asserts the version.
- An **offloaded step** is a step whose `call_id` joins a call recorded on another host. `known_crate_steps` (378-384) and the waiting, kind, test-builds and tests-per-edit sections leave offloaded steps out: they describe natedev.
- `scripts/buildlog/report.py`: new `mac_tests_section(connection, day)`, after `test_builds_section` in `report()` (951-975), omitted when no call that day has `mac` set. Heading `Tests on the Mac`. One row per package: `Package | Tried | Failed on the Mac | Passed there | Mac p50 | natedev p50 | Stayed local`, where `Tried` counts calls with `mac` of `passed`, `passed_filter`, `failed` or `lost`, `Passed there` counts `passed` and `passed_filter`, `Mac p50` is the median `mac_s` of those and `failed`, `natedev p50` is the median natedev time of that day's test calls for the package that ran tests on natedev (`mac` null, `declined`, `lost` or `passed`; never `failed` or `passed_filter`, which ran nothing there), natedev time being `wall_s` less `mac_s` when set, and `Stayed local` lists each decline reason with its count in plain words (`blocked 3, Mac busy 2`). One `Source:` line follows the table. Then two lines:
  - `natedev runs avoided: <N>` — calls with `mac` of `failed` or `passed_filter`, the two that ended on the Mac — `about <X> min of build and test time and <Y> min of waiting, estimated from that day's natedev runs of the same packages` (per package, its count times its `natedev p50`, and its count times the median `wait_s` plus `token_wait_s` of the same calls).
- The three columns are read into one value where the rows leave SQL: `NoMacAttempt`, or `MacAttempt` holding a `MacOffloadOutcome` (`passed`, `passed_filter`, `failed`, `lost`, or `declined` with its reason) and the seconds. The section's code never passes three loose optional values around.
  - `Failed on the Mac, then passed on natedev with --local: <N> (<packages>)` — a `failed` call followed within 30 minutes, same worktree and package, by a call with reason `local_flag` and status 0. Omitted at zero.

**Files:**
- `scripts/buildlog/record.py` — the three call fields.
- `scripts/buildlog/index.py` — columns, defaults, schema version.
- `scripts/buildlog/report.py` — the section and the offloaded-step filter.
- `scripts/buildlog/test_record.py`, `scripts/buildlog/test_index.py`, `scripts/buildlog/test_report.py` — tests.

**Seats:** `1 writer + 1 tester` — record, index and report are one chain; the tests come from this Spec.
- `impl` — `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `scripts/buildlog/report.py`
- `test` — the three test files: the fields round-trip, old records read as null, the schema version, the section's rows and both lines from a fixture with a second host folder `Mac` (the fixture at `test_report.py:50-57`), the section's absence with no Mac calls, the runs-avoided count with a failed and a `passed_filter` call, natedev time leaving `mac_s` out, and an offloaded Mac step staying out of each of Waiting, every kind section, Test builds, Tests per edit and Rebuilds, with one assertion per query path.

**Constraints from prior phases:** `verify.sh` already passes `BUILDLOG_MAC` (`passed` for a Mac pass that natedev then confirmed in the same call, `passed_filter` for a filtered Mac pass that ended there, `failed`, `lost`, `declined`), `BUILDLOG_MAC_REASON` (`off`, `repo`, `linux_only`, `backoff`, `blocked`, `busy`, `unreachable`, `mac_busy`, `battery`, `disk`, `copy`, `local_flag`, or empty) and `BUILDLOG_MAC_S` to `record.py call`. The Mac writes the nextest step itself, with natedev's call id, host `Mac`, and no repo, worktree or sha; it reaches natedev's index with the hourly sync.

**Acceptance gate:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` ends `OK`; `basedpyright scripts/buildlog` ends `0 errors, 0 warnings, 0 notes`.

### Phase 8 — Live on the Mac, then switched on  · status: todo

#### Work Order

**Blocked by:** the showrunner's word that the Ian Hubert demo on the Mac is over (natedev, 2026-10-07). Until then nothing of this feature writes to or runs on the Mac; reached earlier, the run holds here.

**Goal:** A real hana test run goes to the Mac and comes back right in every case, the limits are set from measured numbers, and the feature is switched on.

**Spec:**
This is the first phase that writes to or runs on the Mac. Work in a scratch clone of hana (`git clone --local /home/natepiano/rust/hana <scratch>` at the tip of `init/catalyst`), never in another unit's worktree, with `MAC_TEST_CONFIG` pointing at a scratch copy of the config with `offload=on`. Keep saved output small and delete the clone at the end. Run the checks in order, fix each defect where it lives with a regression test, and record every number in the report back:
1. Each probe line parses on the Mac; a block makes the call stay local.
2. The first and second tree copy: times, and that `target`, `.git` and `.direnv` are absent from the mirror and the Mac's rsync accepts the options.
3. `verify.sh test hana --filter <one test>`: the Mac's cold build time, then its rebuild time after touching `crates/hana/src/tool/surface.rs`, against natedev's.
4. Exit status passes through for a pass, a failing test and a compile error.
5. An interrupt mid-run leaves no cargo, rustc or test process on the Mac and frees the claim.
6. A block placed mid-run is pending, the run finishes, exactly one "Mac is free" message arrives, and the next call stays local. This check flips hana's real `MACOS_CI`, so tell the showrunner before it and keep it to a few minutes: record the variable's value first; while the block is in place `gh variable get MACOS_CI --repo natepiano/hana` prints `false`; after `unblock` it prints the recorded value again, and the skipped list `unblock` printed goes into the report back. Whatever fails in between, put the recorded value back before going on.
7. After `buildlog sync`, the Mac's step is in natedev's index with the call id, and the report's `Tests on the Mac` section shows the run.
8. The full `hana` and `hana_diegetic` suites on the Mac on a green tree: each test that fails only there goes into `mac_skip.hana`; a package with many goes onto `linux_only.hana`.
9. The mirror's build-folder size after both suites sets `mac_budget_gib.hana`; `free_floor_gib` is set so the Mac keeps room (it had 187 GiB free).
10. One call from inside a Codex seat's shell reaches the Mac.
Then set `offload=on` in `config/mac_test.conf` with the measured limits.

**Files:**
- `config/mac_test.conf` — `offload=on` and the measured limits.
- `scripts/mac_test/offload.py` — defects the live run finds.
- `scripts/mac_test/test_offload.py` — a regression test for each.

**Seats:** `2 writers` — nothing splits: the checks are serial against one Mac, so one writer holds the run.
- `impl` — `config/mac_test.conf`, `scripts/mac_test/offload.py`, and every live check
- `test` — opens as impl: `scripts/mac_test/test_offload.py`, a regression test for each defect `impl` reports

**Constraints from prior phases:** `verify.sh test` calls `scripts/mac_test/offload.py run` before the cargo token; `--local` skips it. `mac_test.py` has `status`, `block`, `unblock`; a block switches hana's `MACOS_CI` variable to `false` through `gh` and `unblock` switches it back. The report has a `Tests on the Mac` section. The mirror is `~/.local/state/mac-test/mirror/hana` on the Mac. `scripts/production/mac_run.sh` claims the same lock.

**Acceptance gate:** every check above has a recorded result; `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'` ends `OK`; `basedpyright scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`; `python3 ~/.claude/scripts/mac_test/mac_test.py status` prints `free`, `no block` and `CI's Mac switch: on` at the end, and no scratch clone remains.
