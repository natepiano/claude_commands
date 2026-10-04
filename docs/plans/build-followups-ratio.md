# Build follow-ups: tests per edit — plan

> **Status: running.** The build report tracks tests per edit toward a target, without letting failures go unseen too long.
> **Production: build-followups** — unit `ratio-unit`; production doc `docs/plans/build-followups-production.md`

## Context

- **Source:** the user's adhoc review of 2026-10-04 (natedev session), five follow-up tasks recorded there, Phase 4 of `docs/plans/build-followups.md`, moved here by the showrunner (2026-10-04) to run beside followups-unit; followups-unit's Phase 2 (temp-folder rows, test isolation) also edited `report.py`; its conflicts with this plan were resolved in 01e11dc. The user asked for them to run as a production with natedev as showrunner.
- **Already done, do not redo:** `test --filter` takes several names (`verify.sh` 953ed15), and `commands/unit/delegate.md:360-364` already tells seats to run `check` after each batch of edits and `test --filter` once per finished change.
- **This repo is every session's live configuration.** `~/.claude` main is what every Claude session on natedev and the Mac loads. Edit only this worktree. Run the worktree's copies (`./scripts/...`), never `~/.claude/scripts/...`, when testing a change.
- **The build log** lives in `~/.local/state/buildlog` (`scripts/buildlog/store.py`; `BUILDLOG_DIR` moves it). `buildlog schema` lists its tables (steps, tests, calls, ci_runs, ci_jobs, ci_steps, samples) and columns; `buildlog query "<SQL>"` reads it. Read the real log freely, but never write to or delete from it in a test.
- **Times** in every report and section carry their zone (EDT), and their date when not today.

## Delegation Context

- **Project:** `~/.claude` (commands, skills and scripts; Python 3.13 and shell, no Rust).
- **Project started:** 2026-10-04T16:13:27+00:00
- **Layout:** `scripts/delegate/verify.sh`; `scripts/buildlog/{report,record,store,index,sync,cli,ci,treekey,sample,disk,rust_release}.py` with `test_*.py` beside them; `scripts/validate_and_push/validate_and_push.sh`; `commands/showrunner/produce.md`; `commands/unit/delegate.md`.
- **Test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` from the worktree root; `test_index` sets `BUILDLOG_DIR` on import.
- **Lint:** `basedpyright scripts/buildlog` with zero errors and zero warnings, and `bash -n` (or `zsh -n` for a zsh script) on each changed shell script.
- **Invariants:**
  - Python (user rules): annotate every signature; no `Any` (a `TypedDict` for dicts with known keys); no file-level type ignores; `uv pip install`, never `pip install`.
  - A test never writes the real build log (`~/.local/state/buildlog`) or touches the user's state.
  - A Rust build for testing `verify.sh` runs in a `git clone --local` under `/tmp`, never in another session's worktree or in `~/rust/hana`; hana links only inside its nix devShell (`nix develop`).
  - Every new report section states its source and is no longer than it needs to be: one table and at most one line under it.
  - `report.py` keeps one style: `table()`, `seconds()`, `count()`, `gib()`.

## Phases

### Phase 1 — Tests per edit · status: done

#### As-built

- `report()` renders a "Tests per edit" section from `tests_per_edit_section(connection, day) -> list[str]`, after the test-build, port-lint and CI sections and before the summaries.
- `tests_per_edit_data(connection, end_day) -> TestsPerEditWindow` walks every `verify.sh` call through the report day in `started_at` order, per seat = `coalesce(nullif(delegate_session,''), nullif(session,''))`. `call_trees(connection, end_day) -> dict[str, KnownCallTrees]` gives each call its first and last known `steps.tree_key`, joined to `calls` by `steps.call_id` for `tool='verify.sh'`.
- An edit is a difference between the tree a seat's call ended on (its last step with a known key) and the tree its next call started on (first known key), so a lint call's own formatting rewrite is not an edit. A call with no known key is skipped and does not break the chain. Chains run over the whole log through the report day; tests, edits and failures count only inside the 7-day window.
- Tests are `verb = 'test'` calls, whole and `--filter`. Green = status 0 and outcome `ran` or `reused`; it resets the seat's edits since green and closes its open failures. Failure = outcome not `interrupted`, and outcome `failed` or a nonzero status.
- Trend table: 7 day rows (`DailyTestsPerEdit`, all seats summed), newest first: Day, Tests, Edits, Tests/edit, Target. `target_status(activity) -> str` gives `—` (no edits), `on target`, `above` or `below`, comparing ratio and target at two decimals; 0.5 is a sweet spot, not a ceiling.
- Bins table: `EDIT_BINS = ("0", "1", "2–3", "4–7", "8+")`, chosen by `failure_bin(edits: int) -> int`, each a `FailureRecoveryBin` (failures, recovered, minutes_to_green) shown as Failures and Avg to next green. The `0` bin holds failures right after a green. Bins cover the same 7 days as the trend; a failure with no later green counts in its bin but not in its average, and the line under the table gives how many.
- Each table has one source line naming the window `{first_day}–{day}`.
- `TESTS_PER_EDIT_TARGET = 0.5`; its comment gives the source: the 2026-10-01–04 log, average minutes to green 1 edit 12.54, 2–3 16.09, 4–7 20.94, 8+ 31.28; one test per two edits keeps most gaps within 2–3, before the climb at 4–7.

**Files:**
- `scripts/buildlog/report.py` — the section, its data pass (`TestsPerEditWindow`), the target constant
- `scripts/buildlog/test_report.py` — behavior tests for edits, ratio, bins, target labels and rendering

**Gotchas:**
- `calls` has no tree key; only `steps` does.
- 137 backfilled test calls carry outcome `failed` with status NULL, which is why the failure test checks outcome as well as status.
- `cli.py report` and `cli.py query` run `index.update()`, which writes the live index; the read-only path to the real log is `index.read_only()`.
- basedpyright exits 3 on every checkout because `pyrightconfig` names a missing `.venv`; the bar is 0 errors, 0 warnings.

**Ruled out:**
- A tree key column on `calls`: the `steps` join supplies it with no schema change, so `index.py` and `record.py` are unchanged.

### Phase 2 — Know when a new Rust is out, and what adopting it costs · status: done

#### As-built

- `rust_release.check_release()` runs third in `cli.py` `hourly()`, after sync and ci and before `index.update()`, on the existing `buildlog hourly` user timer (every 3600 s from `~/.claude`, only `PATH` set). It returns 0 unless something failed.
- Pin: `[toolchain] channel` from `git -C ~/rust/hana show origin/init/catalyst:rust-toolchain.toml`, read-only; hana's checkout is never fetched or touched. A two-part pin `X.Y` covers every `X.Y.z`; versions compare as integer tuples. A missing file or a channel name is no pin: nothing shown or sent, and the record stays with `pin` None, so a returning pin does not re-trial or re-text.
- Once per local day the check fetches `https://static.rust-lang.org/dist/channel-rust-stable.toml` (`urllib`, 30 s) and parses it with `tomllib`: version = first word of `pkg.rust.version`, date = top-level `date`. A failed fetch goes to `store.note_error`, the run keeps working from the saved release (pin refresh, trial, text), returns 1 and fetches again next hour.
- A stable newer than the pin is pending until its trial runs. The trial starts only in quiet hours 02:00–05:00 local, with no file in `~/.local/state/build-hold/` (`/build_hold` writes one per holder: holder, ISO time, what for; release removes it), empty output from `pgrep -u "$USER" '^(cargo|rustc|cargo-nextest)$'`, and free space on the home filesystem (`os.statvfs`) of at least the floor plus `TRIAL_HEADROOM_GIB = 50`. The floor is `sweep_free_floor_gib.<host>`, else `sweep_free_floor_gib`, in `config/lint.conf` (first value wins; no key, no floor). Otherwise the state holds `TrialWaiting` with the reason, and a later hourly run tries again.
- Each trial step is a subprocess in its own process group with a timeout that kills the group (toolchain 30 min, clippy 2 h, mend 1 h); `CARGO_MAKEFLAGS="--jobserver-auth=fifo:/dev/steve"` is set when unset and `/dev/steve` is a readable, writable character device. Steps: `rustup toolchain install <ver> --profile minimal --component clippy --component rustc-dev` (left installed for the bump); `git clone --local --no-checkout ~/rust/hana` into `rust-release-trial-<ver>` under the temp dir, detached at the `origin/init/catalyst` sha.
- Clippy in the clone: `nix develop .#ci -c cargo +<ver> clippy --workspace --all-targets --all-features --message-format=json` with `CARGO_INCREMENTAL=0`, the mold link flag and no `-D warnings`. `compiler-message` warnings and errors are counted per crate, deduplicated by package, level, lint code and primary span; every warning counts as new. Compiler errors finish the trial with an error count, and mend still runs.
- Mend: `RUSTC_BOOTSTRAP=1 CARGO_TARGET_DIR=<clone>/mend-target cargo +<ver> install --path ~/rust/cargo-liner/crates/cargo-mend --root <clone>/mend-root`; exit 0 is `cargo-mend builds`, anything else the result `cargo-mend does not build`. The sizes of `<clone>/target` and `<clone>/mend-target` in GiB go into the state to calibrate the headroom, and a `finally` deletes the clone on every outcome.
- The hold is checked after every step; a hold arriving mid-trial stops it, deletes the clone and leaves the version pending. Any other step failure or timeout gives `TrialFailed` with the step and its first error line. A failed trial is not retried.
- State: `store.root() / "rust_release.json"`, written atomically (`.tmp`, then `replace`). `ReleaseState` holds the check day, latest stable and its date, `pin: str | None` (None: hana has no pin), the outcome `TrialWaiting | TrialFinished | TrialFailed`, the target sizes and whether the text was sent.
- Text, once per version, at the end of the first quiet-hours run after the version is found: `scripts/notify/pushover.py --priority 0` as a subprocess, title `Rust 1.100.0 out`, message `Rust 1.100.0 out: 7 new warnings in 3 crates, cargo-mend builds. Tell natedev bump or wait.`; errors are named (`2 errors in 1 crate`). A waiting trial sends its reason instead (`trial waiting: 513 GiB free, needs 550`), and the later result reaches only the report. A send error goes to `store.note_error`; `~/.config/pushover/env` is never read.
- `report_line()` adds to `report()`'s one-line block while a stable newer than the pin is out: `Rust 1.100.0 out since 11-12; hana on 1.99.0. Trial: 7 new warnings in 3 crates, cargo-mend builds.`, or `Trial: waiting, <reason>.` / `Trial: failed, <reason>.`. No pin, no state, or a pin that caught up gives an empty list.

**Files:**
- `scripts/buildlog/rust_release.py` — the daily check, trial, text, state types and `report_line()`
- `scripts/buildlog/cli.py` — `hourly()` runs the check after sync and ci
- `scripts/buildlog/report.py` — includes `rust_release.report_line()` in the one-line block
- `commands/build_hold.md` — hold writes a per-holder file, release removes it
- `scripts/buildlog/test_rust_release.py`, `scripts/buildlog/test_report.py` — behavior tests with fakes; the report line's three forms and its absence

**Gotchas:**
- hana has no `rust-toolchain.toml` on `origin/init/catalyst` yet, so the check stays silent until one lands.
- natedev has 528 GiB free against 550 needed (500 floor + 50 headroom), so a trial today waits on disk.
- The trial runs inside the hourly job, so sync and CI wait while it runs (up to 3.5 h, at night only).
- hana's `.#ci` shell carries no Rust toolchain; rustup's is used, and the job `PATH`'s `cargo` is the rustup proxy, which is why `cargo +<ver>` works from the hourly job.
- Tests never touch hana, the network or the real state: every outside effect (fetch, pin read, subprocess runner, clock, free space, hold check, pgrep, text sender) is a parameter whose default is the real one, and tests pass fakes.

**Ruled out:**
- Trialing a release a newer stable has superseded: never the bump target.
- `-D warnings` and CI's windows-gnu clippy in the trial: every Linux warning gets counted, and CI already holds the pin at zero.
- One shared hold marker: overlapping holds need a file per holder.

