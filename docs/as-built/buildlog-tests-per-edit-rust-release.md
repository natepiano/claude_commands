# Build log: tests per edit and the Rust release check

## What it is

Two additions to the build log. The first is a "Tests per edit" section in the daily `buildlog report`. It counts how many `verify.sh test` runs each agent seat makes per edit to its tree, compares that to a target of 0.5, and shows what testing too rarely costs: failures binned by edits since the last green test, with the average time back to green. It answers "are seats testing often enough?" without hiding the failures behind a single ratio. The second is a Rust release check on natedev's hourly build-log job. Once a day it reads the latest stable Rust and the toolchain hana pins. When a newer stable is out, it trial-builds hana with it at night, in a throwaway clone, while nothing else is building. It then texts the user once per version with the cost of adopting it: new clippy warnings, compiler errors, and whether cargo-mend still builds. The same result appears as one line in the daily report until hana's pin catches up.

## How it works

### Key files

- `scripts/buildlog/report.py`: `tests_per_edit_section()`, its data pass `tests_per_edit_data()`, `call_trees()`, `failure_bin()`, `target_status()`, `TESTS_PER_EDIT_TARGET`, `EDIT_BINS`; `report()` adds the section and `rust_release.report_line()`.
- `scripts/buildlog/rust_release.py`: the whole release check: `check_release()`, `trial_release()`, the state types and file, the text, `report_line()`.
- `scripts/buildlog/cli.py`: `hourly()` runs the check after sync and CI.
- `commands/build_hold.md`: `/build_hold` writes one holder file per holder under `~/.local/state/build-hold/`; the check counts any regular file there as a hold. Hold and release are described in `docs/as-built/build-memory-admission.md`.
- `scripts/buildlog/test_report.py`, `scripts/buildlog/test_rust_release.py`: behavior tests on a temporary log, with fakes for every outside effect.
- `/etc/nixos` `modules/linux/buildlog.nix`, `nate.jobs.buildlog`: the hourly timer (3600 s, natedev only, the user's `PATH`) that runs `~/.claude/scripts/buildlog/buildlog hourly`. Not in this repo.

### Tests per edit

**Source.** `calls` rows with `tool = 'verify.sh'`, plus `steps.tree_key` joined through `steps.call_id`. `calls` has no tree key of its own.

**Seats.** A seat is `coalesce(nullif(delegate_session,''), nullif(session,''))`. Several delegate seats can share one session, so the delegate session wins. A call with neither is skipped.

**Trees per call.** `call_trees(connection, end_day) -> dict[str, KnownCallTrees]` gives each verify call the first and last known `tree_key` among its steps, in step order. A call with no keyed step is absent from the map.

**Edits.** `tests_per_edit_data(connection, end_day) -> TestsPerEditWindow` walks every verify call through the report day in `started_at` order. An edit is a change between the tree a seat's previous call ended on and the tree its next call started on. Any verb (check, lint, test) can reveal an edit. A lint call that rewrites the tree itself (formatting, mend fixes) changes it inside the call, so that rewrite is not counted. A call with no known tree is skipped and does not break the seat's chain.

**Window.** The chains run over the whole log through the report day, so the first call in the window is compared with the call before it. Tests, edits and failures are counted only inside the 7-day window, `day − 6` through `day`, by local date.

**Tests, greens, failures.** Tests are `verb = 'test'` calls, whole-package and `--filter` alike.
- Green: status 0 and outcome `ran` or `reused`. It resets the seat's edits since green and closes every open failure for that seat.
- Failure: outcome not `interrupted`, and outcome `failed` or a non-null nonzero status. It goes into the bin for the seat's edits since green, and waits for the seat's next green.
- An interrupted test counts as a test but is neither green nor failure.
- Minutes to green run from the failing call's end to the green call's end (`ended_at`, else `started_at`).

**Rendering.** `tests_per_edit_section(connection, day) -> list[str]` gives `### Tests per edit` and two tables, each with one source line:
1. Trend: seven `DailyTestsPerEdit` rows, newest first, all seats summed. Columns Day, Tests, Edits, Tests/edit, Target. The ratio has two decimals, or `—` with no edits. `target_status()` gives `—`, `on target`, `above` or `below`, comparing ratio and target at two decimals. Line: `Source: verify.sh test calls and step tree keys, <first>–<day>; target 0.5 tests/edit; — means no observed edit.`
2. Bins: one `FailureRecoveryBin` (failures, recovered, minutes_to_green, recovery_minutes) per `EDIT_BINS = ("0", "1", "2–3", "4–7", "8+")`, picked by `failure_bin(edits)`. The `0` bin holds failures with no edit since the last green. Columns Edits since green, Failures, Avg to next green, p95 to next green (nearest rank over `recovery_minutes`). A failure with no later green counts in Failures but not in the average or the p95. Line: `Source: verify.sh failed test calls and step tree keys, <first>–<day>`, plus `; N without a later green` when there are any.

`report()` places the section after CI and before the Disk section and the summaries.

### Rust release check

**Hook.** `cli.hourly()` runs `sync.sync`, `ci.ci`, then `rust_release.check_release`, each in its own `try`. A job that raises prints one line, goes to `store.note_error("hourly <name>")` and sets exit 1; the others still run. `index.update()` runs last. The check runs with all defaults:

```python
def check_release(*, fetch_channel=fetch_channel_file, read_pin=pinned_version,
                  run_command=run_process, now=datetime.now, free_gib=free_space_gib,
                  held=build_hold_active, building=cargo_building,
                  send_text=send_release_text, trial_parent=default_trial_parent) -> int
```

It returns 0, or 1 when the channel fetch or the text failed.

**Pin.** `pinned_version()` runs `git -C ~/rust/hana show origin/init/catalyst:rust-toolchain.toml` and reads `[toolchain] channel`. hana's checkout is only read: never fetched, checked out or built in. `version_key()` accepts two or three ASCII decimal parts and compares them as integer tuples. Anything else (a missing file, `stable`, `nightly-2026-10-01`) is no pin. With no pin the run returns 0 at once: no fetch, no trial, no text. If a state file exists, its `pin` becomes `None` and the rest of it is kept.

**Two-part and three-part pins.** `newer_than_pin(stable, pin)` cuts the stable version to the pin's length before comparing. A pin of `1.99` covers every `1.99.z`, since rustup already takes the latest patch for it. A pin of `1.99.0` does not cover `1.99.1`.

**Daily fetch.** When there is no state, or its `check_day` is not today's local date, the run fetches `https://static.rust-lang.org/dist/channel-rust-stable.toml` with `urllib` (30 s) and parses it with `tomllib`. Version is the first word of `pkg.rust.version`; date is the top-level `date`. A fresh record keeps the saved trial only if its version matches, otherwise it starts `waiting` with reason `awaiting quiet hours`. It keeps `text_sent` only if the stable version is unchanged. A failed fetch goes to `store.note_error`. With no saved state the run returns 1. With saved state it carries on from the saved release (pin refresh, trial, text) and returns 1, and `check_day` stays old, so the next hour fetches again.

**Trial gate.** A stable newer than the pin is pending while its trial is `waiting`. A trial starts only when all of these hold:
- the local hour is 2, 3 or 4 (02:00–05:00);
- `build_hold_active()`: no regular file in `~/.local/state/build-hold/`;
- `cargo_building()`: `pgrep -u <uid> '^(cargo|rustc|cargo-nextest)$'` prints nothing (a pgrep error counts as building);
- `free_space_gib()` (`os.statvfs` on the home) is at least the floor plus `TRIAL_HEADROOM_GIB`. `free_floor_gib()` reads `sweep_free_floor_gib.<host>`, else `sweep_free_floor_gib`, from `config/lint.conf`, the first value of a key winning, as the sweep reads it.

Otherwise `waiting_reason()` returns the first failing reason (`build hold active`, `cargo build active`, `513 GiB free, needs 550`) and the state holds `TrialWaiting`. A later hourly run tries again. Only `waiting` trials run: a finished or failed trial is never repeated for the same version.

**Trial steps.** `trial_release(version, run_command, held, trial_parent) -> TrialOutcome` uses the clone `<temp dir>/rust-release-trial-<version>`. It removes any stale clone first, and a `finally` removes the clone on every outcome.

| Step name | Command | Timeout |
| --- | --- | --: |
| `toolchain` | `rustup toolchain install <ver> --profile minimal --component clippy --component rustc-dev` | 1800 s |
| `clone` | `git clone --local --no-checkout ~/rust/hana <clone>` | 600 s |
| `revision` | `git -C ~/rust/hana rev-parse origin/init/catalyst`, then `git -C <clone> checkout --detach <sha>` | 30 s, 600 s |
| `clippy` | in the clone: `nix develop .#ci -c cargo +<ver> clippy --workspace --all-targets --all-features --message-format=json`, with `CARGO_INCREMENTAL=0` and `CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C link-arg=-fuse-ld=mold"` | 7200 s |
| `cargo-mend` | in the clone: `cargo +<ver> install --path ~/rust/cargo-liner/crates/cargo-mend --root <clone>/mend-root`, with `RUSTC_BOOTSTRAP=1` and `CARGO_TARGET_DIR=<clone>/mend-target` | 3600 s |

- `run_process()` starts each step in its own session (`start_new_session=True`). On timeout it sends `SIGKILL` to the whole process group, then raises `TimeoutExpired`.
- `trial_environment()` copies the environment. It sets `CARGO_MAKEFLAGS="--jobserver-auth=fifo:/dev/steve"` when that variable is unset and `/dev/steve` is a readable, writable character device (natedev's jobserver, `modules/linux/jobserver.nix`).
- `held()` is checked before and after every step. A hold returns `TrialWaiting` with `build hold active`, deletes the clone, and leaves the version pending.

**Trial results.**
- `diagnostic_counts(stdout)` reads the clippy JSON. It counts `compiler-message` entries at level `warning` or `error`, once per (package id, level, lint code, primary span), and returns warnings, warning crates, errors, error crates. Every warning counts as new.
- Clippy exiting nonzero with compiler errors still finishes the trial, with the error count, and mend still runs. Nonzero with no compiler error is a `TrialFailed`.
- Mend exit 0 is `cargo-mend builds`; anything else is `cargo-mend does not build`. Either way the trial is `TrialFinished`.
- `trial_step_was_killed()` reads a step as killed when it exits 128 or more, -9 or -15, or prints `SIGKILL`, `SIGTERM` or `signal 9`/`15`. A killed step (clippy included, whatever its error count) leaves the trial `TrialWaiting` with `<step> killed; retry next night` (the checkout reads `checkout killed`), so the next quiet-hours run trials again.
- Any other nonzero step gives `TrialFailed` with the step name and `first_error()`. That is the first stderr line containing "error", else the first rendered line of a JSON compiler error, else the first stdout line containing "error", else the first non-empty line, else `exit N`. An `OSError` or `SubprocessError` (a timeout included) gives `TrialFailed` with the step and the exception's first line.
- Finished and failed trials record `target_gib` and `mend_target_gib`, the summed file sizes of `<clone>/target` and `<clone>/mend-target`, to calibrate the headroom.

**Text.** In quiet hours, after the gate or the trial, a version whose text is not yet sent gets one. `send_release_text()` runs `python3 scripts/notify/pushover.py --priority 0 <title> <message>` (30 s). Success is exit 0. Title `Rust 1.100.0 out`; message `Rust 1.100.0 out: <result>. Tell natedev bump or wait.` `trial_text()` gives the result:
- finished: `7 new warnings in 3 crates, cargo-mend builds`, with `, 1 error in 1 crate` before the mend part when there are errors;
- waiting: `trial waiting: 513 GiB free, needs 550`;
- failed: `trial failed: clippy: <first error>`.

If the first quiet-hours run could not trial, the text carries the waiting reason, and the later result reaches only the report. A send error or nonzero exit goes to `store.note_error`, returns 1, and leaves `text_sent` false, so the next quiet-hours run sends again. This module never reads `~/.config/pushover/env`; the sender owns its credentials.

**State.** `ReleaseState` (`check_day`, `stable_version`, `release_date`, `pin: str | None`, `trial: TrialWaiting | TrialFinished | TrialFailed`, `text_sent`) lives in `store.root() / "rust_release.json"`, which `BUILDLOG_DIR` moves. `write_state()` writes `rust_release.json.tmp`, then renames it over the file. `read_state()` returns `None` for a missing or unreadable file.

**Report line.** `report_line() -> list[str]` is the last entry in `report()`'s one-line block, after the calls, port-lint and CI lines. While a stable newer than the pin is out it gives one line:

`Rust 1.100.0 out since 11-12; hana on 1.99.0. Trial: 7 new warnings in 3 crates, cargo-mend builds.`

The trial part is `waiting, <reason>` or `failed, <step>: <reason>` for the other outcomes. No state, a `None` pin, or a pin that caught up gives an empty list.

## Invariants

- Every outside effect of `check_release()` is a keyword parameter whose default is the real one: channel fetch, pin read, subprocess runner, clock, free space, hold check, cargo check, text sender, trial folder. A new outside effect gets a new parameter, and tests pass a fake.
- Tests never touch the real build log, the real state file, hana, the network, `~/.local/state/build-hold/` or the user's config. `point_root_at()` points `BUILDLOG_DIR` at a temporary root; `LINT_CONFIG` and `tempfile.gettempdir` are patched. The only real subprocess in the suite is the `sh -c 'sleep 60 &'` that checks the group kill.
- hana is only read: `git show` and `git rev-parse`. All building happens in a `git clone --local` under the temp dir, and the clone is deleted on every outcome, a hold included.
- One text per stable version. The trial and `text_sent` carry across days while the version is unchanged. A newer stable resets both. A pin that goes away keeps both, so a returning pin neither re-trials nor re-texts.
- A failed trial is not retried. Only a `waiting` trial runs, and a killed step leaves the trial `waiting`.
- A trial never starts outside 02:00–05:00 local, during a build hold, while the user runs cargo, rustc or cargo-nextest, or below floor plus headroom.
- The state file is written atomically. The check exits 1 only for a failed fetch or a failed text, and one hourly job raising never stops the others.
- Tests-per-edit chains start before the window. Narrowing the query to the 7 days would miscount the first call of each seat.
- Each report section names its source and has at most one line under each table. `report.py` keeps one style: `table()`, `seconds()`, `count()`, `gib()`.
- `cli.py report` and `cli.py query` run `index.update()`, which writes the live index. Code that only reads the real log uses `index.read_only()`.
- Python: every signature annotated, no `Any` (a `TypedDict` for known keys), no file-level type ignores.

## Calibration / gotchas

- `TESTS_PER_EDIT_TARGET = 0.5`. From the 2026-10-01–04 log, average minutes to green were 12.54 after 1 edit, 16.09 after 2–3, 20.94 after 4–7 and 31.28 after 8+. One test per two edits keeps most gaps within 2–3, before the climb at 4–7. Ratio and target are compared at two decimals.
- The window is 7 days and the bins are `0`, `1`, `2–3`, `4–7`, `8+`.
- 137 backfilled test calls carry outcome `failed` with status NULL. That is why the failure test checks outcome as well as status.
- A seat that has never had a green counts its edits since green from its first call in the log.
- Quiet hours are `2 <= hour < 5`, local time: at most three hourly runs a night can start a trial.
- `TRIAL_HEADROOM_GIB = 50`. natedev's floor is `sweep_free_floor_gib.natedev=500`, so a trial needs 550 GiB free. natedev had about 517–528 GiB free on 2026-10-04, so a trial would wait on disk. No floor key means a floor of 0. An unparseable value also means 0 here, where the sweep fails instead.
- Timeouts: channel fetch 30 s, pin read 30 s, pgrep 10 s, text 30 s; trial steps as in the table. The trial steps can run up to about 3 h 50 min.
- hana had no `rust-toolchain.toml` on `origin/init/catalyst` on 2026-10-04, so the check stays silent until one lands.
- The pin is whatever `origin/init/catalyst` was at hana's last fetch. The check never fetches hana.
- The trial runs inside the hourly job, so sync, CI polling and the index update wait while it runs, at night only.
- The hold is checked between steps, not during one. A hold that arrives during clippy waits for clippy to end, up to 2 h. `/build_hold` already waits for `pgrep` to print nothing before its test starts.
- hana's `.#ci` shell carries no Rust toolchain. The job `PATH`'s `cargo` is the rustup proxy, which is why `cargo +<ver>` selects the trial toolchain inside `nix develop`.
- The clone has no `origin/init/catalyst` ref. The sha is resolved in hana and checked out by sha in the clone; that works because `--local` hard-links hana's whole object store.
- The trial toolchain stays installed for the bump. Nothing removes old trial toolchains.
- The target sizes are summed apparent file sizes, not allocated blocks. They exist only to calibrate the headroom.
- A finished trial with no warnings still says `0 new warnings in 0 crates`.
- Failure reasons name the step: `toolchain`, `clone`, `revision` (rev-parse or checkout), `clippy`, `cargo-mend`.
- Deleting `rust_release.json` forgets the trial and the sent text. The next run fetches, and the next quiet-hours run trials and texts again.
- The check runs on natedev only, and sync does not copy the log root, so the Mac's report never shows the Rust line.
- The hourly job runs `~/.claude` main's copy. Test a change with the worktree's `./scripts/...`.
- basedpyright exits 3 on every checkout because `pyrightconfig` names a missing `.venv`. The bar is 0 errors and 0 warnings, not the exit code.

## Why

- **Edits come from tree keys at call boundaries.** A seat's edits are what changed between its calls. Comparing one call's last tree with the next call's first tree leaves out a lint call's own rewrite. `steps` already carries `tree_key`, so no schema change was needed in `index.py` or `record.py`.
- **Failures sit beside the ratio.** A ratio alone could look fine while failures went unseen for many edits. The bins show what each gap length costs in time to green, and they are the data the target was set from. 0.5 is where recovery time stays low, not a ceiling, so `above` is not a fault.
- **Interrupted runs are neither green nor failure.** They say nothing about the code.
- **The trial mirrors CI's Linux clippy without `-D warnings`.** CI already holds hana at zero warnings under the pin, so every warning under the new release is new, and dropping `-D warnings` lets the build count all of them. CI's windows-gnu clippy is left out of the trial; the Linux run shows the cost.
- **cargo-mend is trialed too.** It is built on `rustc_private`, so a new rustc can break it. The `rustc-dev` component and `RUSTC_BOOTSTRAP=1` let a stable toolchain build it.
- **Only the latest stable is trialed.** A release a newer stable has superseded is never the bump target.
- **Night, no hold, no cargo, enough disk.** The trial is a full workspace build. It must not compete with agents' builds or a held test, and must not take the disk below the floor the sweep defends (natedev's disk filled on 2026-10-03).
- **Process-group kill.** cargo and nix start child processes. Killing only the parent on timeout would leave rustc running.
- **One hold file per holder.** Holds can overlap. With one shared marker, the first release would end a hold another session still needs.
- **A killed step is not a verdict.** earlyoom or a cgroup limit can kill the trial for reasons that say nothing about the release, so it waits for another night instead of recording a failure.
- **One text per version, with the decision left to the user.** The text asks "bump or wait". A waiting reason is sent rather than nothing, so the user knows the trial has not run, and the later result goes only to the report to avoid a second night-time text.
- **The pin is read with `git show` from `origin/init/catalyst`.** That reads the pin without touching any checkout.
- **Two-part pins cover patch releases.** rustup already takes the latest patch for a two-part channel, so a patch release needs no bump.
- **A missing pin keeps the record.** If the toolchain file goes away for a while, its return should not re-trial or re-text a release already handled.
- **One fetch a day, retried hourly.** Stable releases are weeks apart, so a daily read is enough, and a failure is retried at the next hour.
- **The check lives in the existing hourly job.** No new timer or unit. The cost is that sync and CI wait during a night-time trial.
- **The text goes through `pushover.py` as a subprocess.** The sender owns its credentials, so this module never reads them.
- **State sits in the build-log root.** `BUILDLOG_DIR` moves it with the log, which keeps tests off the real file.
