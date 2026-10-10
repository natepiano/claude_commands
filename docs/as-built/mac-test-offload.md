# Mac test offload

## What it is

`verify.sh test` runs a package's nextest suite on the Mac first when the Mac is free, then confirms on natedev, which stays the gate. A showrunner (or any session) can block the Mac for its own work; a block also turns off hana's CI use of the Mac, lifts by itself, names the CI runs it made skip macOS, and sends its holder one "Mac is free" message when the Mac's current work ends. The block shows in every showrunner footer and dailies report. The build log and the builds report record what each call did on the Mac.

## How it works

### State, lock and block — `scripts/mac_test/mac_test.py`

- An `argparse` CLI (`claim`, `release`, `block`, `unblock`, `status`, `watch`) over a state directory, `MAC_TEST_STATE_DIR` (default `~/.local/state/mac-test/`). Records are written to a temp name, then `os.replace`. `state_paths()` names the files; `read_block(path) -> NoMacBlock | PendingMacBlock | ActiveMacBlock` decodes `block.json` and raises `ValueError` on a file it cannot decode. Both are importable with no side effects.
- Three locks: `state.lock` covers `claim` and `release` and never waits on `gh` or a message; `control.lock` covers `block`, `unblock`, `status` and each watcher pass; `watch.lock` keeps one watcher (a `systemd-run --user` unit named `mac-test-watch-<pid>`) at a time.
- `run.json` is `{"pid", "proc_start", "what", "worktree", "since"}`; a run is live only while `/proc/<pid>/stat` field 22 equals `proc_start`.
- `claim --pid PID --what TEXT [--worktree PATH] [--wait SECONDS]` exits 0 `claimed`, 10 `blocked by <holder>: <for>`, 11 `busy: <what> since <HH:MM>`, 12 `state unreadable: <path>`, 1 for a pid that is not running. `release --pid PID` removes its own `run.json`.
- `block --holder NAME --for TEXT [--hours N] [--showrunner NAME]` records the session from `CLAUDE_CODE_SESSION_ID`. A block lasts `block_hours`, at most `block_max_hours`; the holder is warned `block_warn_minutes` before it lifts by itself. A repeat block by the same holder keeps `since`. `unblock --holder NAME` lifts only the holder's own block.
- **CI switch.** `block` turns the repository variable `ci_variable` (hana's `MACOS_CI`) off through `gh` and records a `ci` word: `off_by_this_block`, `off_before`, `not_configured`, `still_on`, `off_unconfirmed`. Unblock and expiry turn it back on only for `off_by_this_block` and `off_unconfirmed`, and list per branch the CI runs that skipped macOS meanwhile, with the `gh workflow run` line to rerun them. A lift that cannot turn the switch back on keeps the block, messages the holder hourly and keeps trying. A new `ci` word goes into `CiSwitch`, `decoded_ci`, `ci_needs_restore` and `ci_may_still_be_on`.
- **Pending until idle.** A block is pending while a local run is live, or while `ci_activity` reads busy: a queued or in-progress run of `ci_workflow` is busy until it lists a job named `ci_job` with status `completed`. A pending block says `a CI run may still use the Mac`; the stored `waiting_on` kind is `ci_job`.
- **The message.** Sent once per block, keyed `mac-free-<since>`, through `send.py` to the session's socket (from `sessions.py socket session:<id>`) or the holder's name: `Message from mac-test: the Mac is free. <what> ended at <HH:MM zone>; your block (<for>) is active, and nothing is built or tested there until you run unblock.` For CI the ended work reads `CI's hold on the Mac`. send.py exit 0 or 1 counts as delivered.
- Every `gh` call is bounded by `gh_timeout_s`; the send and the session lookup by 30 s (`MAC_TEST_MESSAGE_TIMEOUT_S`). An empty `ci_repo` turns every `gh` call off.
- `commands/mac_test.md` is `/mac_test block <why>`, `unblock`, `status`.

### Footer and dailies — `scripts/production/dailies_render.py`

A pending or active block is one footer item after the build-hold lines: `Mac block: <holder> since HH:MM ZZZ, for <reason> - lifts Ddd HH:MM ZZZ` (or `Mac block pending: …`), ending ` - CI can still use the Mac` while `ci_may_still_be_on`. The renderer reads `block.json` through `read_block(state_paths().block)` alone: no lock, no `gh`, no message. An unreadable file shows as `Mac block: its state file cannot be read (<path>)`.

### The offload runner — `scripts/mac_test/offload.py`

- `offload.py run --repo-root DIR --package PKG --call-id ID [--filter-run] [--result FILE] -- <nextest words>` exits 0 when the run passed on the Mac, 75 when the caller must run locally, and the Mac run's own status when tests failed there. `write_result()` writes `mac=`, `reason=`, `seconds=` from one `MacOffloadResult`: `PassedOnMac | FailedOnMac | NoTestMatchedOnMac | LostMacRun | DeclinedMacRun`. Only that file's `mac` word tells a Mac test failure from a killed runner; a missing or contradictory file is no usable result.
- Declines, each printing `mac_test: staying on natedev (<reason>)`, in order: `off`, `repo`, `linux_only`, `backoff`; the claim (`blocked`, `busy`, `state`); the probe (`unreachable`, which writes the back-off file, `mac_busy`, `battery`, `disk`); `copy`.
- The probe counts `cargo`, `rustc` and `cargo-nextest` processes, the one-minute load (against `max_load`), power and free GiB (against `free_floor_gib`).
- The copy is `rsync -rlpc --delete --exclude-from` into `~/.local/state/mac-test/mirror/<repo>`, leaving out `/.git`, `/target/` and every ignored path.
- The run is one ssh call through `lint nextest`, `mac_skip.<repo>` added to the `-E` filter and `LINT_SWEEP_BUDGET_GIB` set from `mac_budget_gib[.<repo>]`. Output streams as it arrives; only an exact `mac_exit=<n>` line is held back as the status. Nextest exit 4 is `NoTestMatchedOnMac`: `mac_test: no test matched on the Mac; running on natedev instead`, exit 75.
- SIGINT, SIGTERM and SIGHUP stop the ssh child, run `pkill -f <mirror>` on the Mac (10 s limit), release the claim and exit 128 plus the signal. A run whose status line never arrives is `LostMacRun`: it runs the same cleanup, prints `mac_test: the link to the Mac dropped; running on natedev instead`, and exits 75 with the time measured after cleanup.
- Every ssh call takes `ssh_options(config)`: `BatchMode=yes`, `ConnectTimeout`, `ServerAliveInterval=15`, `ServerAliveCountMax=3`.

### `verify.sh test` — `scripts/delegate/verify.sh`

- `--local` sets `LOCAL_ONLY` and records `declined` / `local_flag`. Otherwise, when the runner exists, `try_mac_test()` runs between the first cache lookup and the cargo token: `"$PY" "$MAC_RUNNER" run --repo-root <top level> --package <pkg> --call-id "$BUILDLOG_CALL_ID" --result <temp file> [--filter-run] -- "${NEXTEST_ARGS[@]}"` (`compose_nextest_args()` builds the words; `VERIFY_MAC_RUNNER` substitutes the runner in tests).
- `declined` and `lost` run on natedev. No usable result is recorded as `declined` / `runner` and runs on natedev. A full `passed` is confirmed by the natedev run in the same call (`verify.sh: passed on the Mac (macOS); now confirming on natedev, which is the gate.`). A `passed` on a `--filter` run records `passed_filter` and exits 0 with nothing run on natedev. `failed` records the call and exits with the runner's status.
- A signal is forwarded to the runner (`forward_mac_runner_signal()`) and waited for; the exit is 128 plus the signal, with no call record.
- Each call record carries `mac`, `mac_reason`, `mac_s` from `BUILDLOG_MAC`, `BUILDLOG_MAC_REASON`, `BUILDLOG_MAC_S`.

### The showrunner's Mac build — `scripts/production/mac_run.sh`

It claims the Mac after its reach probe (`mac_test.py claim --pid $$ --what "mac run <sha9>" --wait 600`); claim exit 10, 11 or 12 prints the claim's line and exits 9. `INT`, `TERM` and `HUP` run `stop_mac_run` (one ssh call that ends the clone's builds and test processes and removes its temp files), release, and exit 128 plus the signal.

### Which packages belong on the Mac — `scripts/mac_test/audit.py`

`audit.py <repo dir> [--check]` reads the workspace manifests with `tomllib` (no cargo) and prints `Package | Gated sites (src) | Gated sites (tests) | Target dependencies | Listed | Suggestion`. Predicates from `cfg(…)`, `cfg!(…)` and `cfg_attr(…)` are classed Linux only, same on both, macOS only or unclassified. A Linux-only predicate on a target dependency table means `keep on natedev`; an unclassified one means `needs review`; otherwise `can go to the Mac`. Under the table: `Listing disagreements:`, `Needs review:`, `Not built on the Mac:` (`path:line`). `--check` exits 1 when a `keep on natedev` package is missing from `linux_only.<repo>`, 2 for an unknown repository or unreadable manifest. `commands/mac_test.md` also documents taking the Mac for a job of your own: `mac_test.py claim --pid $$ --what … --wait <s>`, then `release --pid $$`.

### Build log and report — `scripts/buildlog/`

- The index is at schema 11. `calls` carries `mac` (`passed`, `passed_filter`, `failed`, `lost`, `declined`), `mac_reason` and `mac_s`. The Mac's steps carry `caller = 'verify-mac'` (set by the remote command) and reach natedev's index through `buildlog sync`.
- `COUNTED_FAILURE` (`status != 0 AND NOT (caller IS 'verify-mac' AND step IS 'nextest' AND status = 4)`) drives `step_days.failed` and the `failures` view, so a Mac "no tests to run" keeps raw status 4 without counting as a failure.
- `report.py` keeps the Mac's steps out of natedev's tables (`natedev_step_predicate`), leaves out a call whose `mac` is `failed` or `passed_filter` (`natedev_call_predicate`), and decodes each call through `offload_record(mac, reason, seconds_value)`. `mac_tests_section(connection, day)` prints `Tests on the Mac`: `Package | Tried | Failed on the Mac | Passed there | Mac p50 | natedev p50 | Stayed local`, the natedev runs avoided, and `No test matched on the Mac:`.

## Config — `config/mac_test.conf`

`offload=on`; `host=mac`; `free_floor_gib=100`; `max_load=6`; `mac_budget_gib=24` and `mac_budget_gib.hana=56` (size limits of each mirror's build folder, not memory); `linux_only.hana=hana_video,hana_clerestory`; `mac_skip.hana=test(world_panel_words_match_drawn_pixels)`; `ci_repo=natepiano/hana`, `ci_variable=MACOS_CI`, `ci_workflow=ci.yml`, `ci_job=macOS: Compile and Test`; `gh_timeout_s`, `block_hours`, `block_max_hours`, `block_warn_minutes`. A repository needs a `linux_only.<repo>` key, even empty, before it runs on the Mac. `MAC_TEST_CONFIG` selects another file.

## Invariants

- Any doubt means local: every Mac-side trouble except the tests' own exit status falls back to the natedev run, and a worker never waits on the Mac longer than the probe timeout.
- natedev is the gate. Pass records are written only by the natedev run; a Mac pass never stands as one.
- Never trust ssh's exit status (Tailscale SSH returns 0 whatever ran). Status is a printed `mac_exit=<n>` or `rc=<n>` line; ssh's own 255 or a timeout means unreachable.
- Tests run nothing on the Mac: stand-in `ssh`, `rsync`, `gh` on `PATH`, state under `MAC_TEST_STATE_DIR`, config under `MAC_TEST_CONFIG`. A test reaching the footer sets `MAC_TEST_STATE_DIR` too.
- On the Mac the feature writes only under `~/.local/state/mac-test/`. Only step records are written there; call records are written on natedev only.
- The lock lives on natedev, tied to the claiming process: a job that dies frees it, and a job started on the Mac cannot take it.

## Calibration and gotchas

- The mirror copy is `rsync -rlpc`: checksums, no modification times. A touch-only edit never reaches the Mac; time a rebuild with a content edit.
- Measured on hana `46b8c87e5`: Mac cold build 12 min 35 s against natedev's 5 min 39 s; rebuild after a content edit to `crates/hana/src/tool/surface.rs` 37.28 s on the Mac (tree copy 4.2 s) against 52.05 s on natedev. The hana build folder measured 42.18 GiB untrimmed.
- GitHub lists hana's macOS job only after `macOS: Runner Availability` completes, and lists it at once as completed/skipped while the switch is off. A pending block therefore also waits on queued runs that have not reached that job.
- Leftovers from a lost run whose cleanup could not reach the Mac make the next probe read `mac_busy` until they end.
- A non-interactive bash starts an `&` child with `SIGINT` ignored; `offload.py run` restores the default, or a forwarded interrupt would be dropped. A terminal interrupt reaches the runner twice (process group and forward).
- The message and session lookup run outside `state.lock`; send.py can take 40 s.
- A `gh` set that times out after GitHub applied it reads `still_on`, then `off_before` at the next settle. Turning the switch back on after `off_unconfirmed` can undo a hand-made switch-off in between.
- `world_panel_words_match_drawn_pixels` fails only on the Mac, cause not found; it is excluded there and still runs in natedev's confirming run.
- `basedpyright scripts/mac_test` exits 3 on a clean pass (no `.venv`); the result is its last line.

## Why

- A Mac pass is never the gate because the Mac is a different platform and a shared machine; it buys early feedback and spares natedev filtered runs.
- The block turns CI's Mac use off rather than racing it, and waits for CI's job to finish, because a test run and a CI job on the Mac at once corrupt each other's timings and memory.
- One CI rule (busy until the macOS job is listed completed) replaced a two-job rule after live runs showed how GitHub lists the job.
- A lost run keeps no claim when its cleanup fails: the probe's process count already turns the next run away.
- No revision counter on `block.json`: every writer holds a lock. The footer reads `block.json` directly, never `status`, which takes a lock and can call `gh`.



