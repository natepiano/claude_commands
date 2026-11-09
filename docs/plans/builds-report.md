# builds-report

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The build report shows build rates per day before and after each build moment: the new build rules (Moment 1, Moment 2), the natedev build events and the power profile changes, from one list of moments.

> **As-built disposition: amend** — `docs/as-built/buildlog-memory-stalls.md` (report sections); `docs/as-built/build-followups.md` (rebuild bins and the clone-rebuild count)

## Source

2026-10-31 06:5x EDT (the showrunner, the user's request via sdlc)

Track the new build rules as a moment in time, with a before and after report. Moment 1: 2026-10-31 06:50 EDT. The broadcast rules are: fix every visible failure before a rerun; no rerun after a pass until edits are done; `verify.sh check` while editing; lint once at the phase gate. Moment 2: phases-unit's prompt and lint change lands (clippy judge first, `--fix` only if it fails). The showrunner sends that hash.

Baseline, 2026-10-24 to 10-31, from `~/.local/state/buildlog/index.sqlite`:
- hana used 595 of 708 run hours and 1329 of 2125 wait hours on natedev.
- nextest ran 246 steps/day; 46% were resubmitted within 10 minutes, and 62% failed.
- clippy ran 160 steps/day.
- Per phase: 12.9 nextest and 8.4 clippy steps.

Report the same numbers after, split at each moment and at the other events in natedev's build events record and the power profile change. Clone rebuilds (805 runs of 300+ crates, 231 of 327 nextest hours) go on their own row; they are a separate cause under study.

Design rules:
- Extend the one before-and-after comparison that the Power profile section already uses, with the moments in one place. No second copy of it.
- Give per-day rates, because the windows differ.
- Run on demand only, with no notifier.
- Recheck the baseline numbers from the index.

Due: the first report to the showrunner at 07:00 EDT on 2026-11-01, 24 hours after Moment 1, with the caveat that one day is thin. The next report is due 24 hours after Moment 2.

2026-10-31 07:0x EDT (the showrunner)

Use report-unit's definitions:
- A rerun is a step in the same worktree whose start is at most 10 minutes after the previous step's end.
- hana means every repo path except cargo-liner.
- Phases come from progress history.

Print each definition once on the report. Wherever sdlc's number differs from ours, print it beside ours, so the before figure is not silently changed. If sdlc's query arrives and it is a better measure, take it.

2026-10-31 07:1x EDT (sdlc, then the showrunner)

sdlc corrected its rerun figure: 46% → 16% (same folder, next start 0–10 min after the previous end); 62% failed stands. The showrunner's rulings:
1. The rerun row is our 16%. Add 46% as a second row, "queue-hidden repeats": next submit (start − `mem_wait_s`) at most 600 s after the previous end, a different call_id, by folder.
2. The hana set is sdlc's SQL definition (1329 wait hours, 160 clippy/day). Name it in the report, hold it across all windows, and keep one definition in one place.
3. 12.9 and 8.4 are load ratios: week totals 1724 nextest and 1123 clippy over phase_finished events. Compute them the same way after.
4. Add a same-worker rerun row, within 60 minutes: sdlc gives 221 runs and 33.3 h after a pass, and 50–55 h fail then fail.
5. Add the floor sweep e3b8e391 (2026-10-29 14:26 EDT) as a moment: clone rebuild repeats 52.7% → 29.8%, 124 after.

2026-10-31 07:2x EDT (the showrunner)

Do not wait for sdlc's exact keys. Print our definition, our number and sdlc's number beside it. For phases, use natedev only and say so. The fail-then-fail gap (ours 80 h against 50–55) goes on the report as a note.

2026-11-03 15:3x EST (the user, to report-unit, after an interview)

The build report is too long for the user and is not used for analysis. Retire it. Keep all the data. report-unit's job is now a loop like the test improvement work. Each round has five steps:

1. Rank the largest build cost.
2. State a prediction.
3. Fix the cost where it lives.
4. Measure the result.
5. Repeat.
1. Score: phase time, the wall time of each finished phase. Build hours per phase are the second view.
2. Scope: all build capacity (natedev, the Mac, CI, and the AWS build machine when it opens), every repository and production. Rank by cost only.
3. Authority: report-unit fixes buildlog code itself and owns every hypothesis and measurement. A fix in another unit's code goes through the showrunner, and that unit edits. Money (AWS hours), machine changes and worker rule changes go to the user first.
4. Speed: one or two rounds open. A round is measured after at least 10 finished hana phases, plus one more day if the result is not clear. This is a starting point.
5. Reports: keep the showrunner informed; the showrunner and report-unit agree when the user hears. The user always hears a measured change, up or down.
6. Later, lower priority: a way to see the build history as charts, not tables.

## Delegation Context

- **Project:** `~/.claude` — the build log and its report.
- **Project started:** 2026-10-31T07:05:00-04:00
- **Stack:** Python 3.13 (basedpyright strict, unittest).
- **Layout:** `scripts/buildlog/` (index, report, cli).
- **Key files:** `scripts/buildlog/report.py` — `power_profile_section`, `profile_changes` and `profile_step_durations` hold today's one before-and-after comparison; `scripts/buildlog/index.py` — the `steps` and `samples` tables; `scripts/buildlog/cli.py` — commands; `~/.local/state/plan-delegate/runs/*.jsonl` — progress history (`event_type` `phase_finished`, `status` `completed`, `timestamp_epoch`, `phase_instance_id`, `working_dir`).
- **Test lanes:** `scripts/buildlog/test_*.py`.
- **Test:** `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY -u STAFF_OPERATION_FD python3 -m unittest discover -s scripts/buildlog -p '<module>.py'` from the worktree root.
- **Lint:** `basedpyright <the slot's own Python files>`; the footer must read `0 errors, 0 warnings, 0 notes`.
- **Invariants:** build-log `.jsonl` files are appended and never edited. The report reads the index read-only. Never write the live build log or the live progress history.

## Phases

### Phase 0 — The builds merge keeps report-unit's head times on queue-unit's new admission reader  · status: done
**Depends on:** none

#### As-built

- The phase-start merge of origin/builds is in the branch. report-unit's phase 8 work (head times, cargo-token waits, sccache rows) sits on queue-unit's lock-free admission reader (`exclusive_queue`, `gather_queue_inputs` → `QueueInputs` → `queue_read_from`).
- `queue_rows()` owns the one waiter order. `write_head_since()` writes `head_since` and runs under the admission lock.
- `memory_gate.sh` keeps the cargo-token left and back times.
- The tests of both sides stay. The suite runs 514 tests and they pass; basedpyright reads 0 errors, 0 warnings, 0 notes.
- Later merge (builds at 2e272204): resources-unit Phase 26's `resolve_queue_order()` and builds' token wait (`token_left_at` on the waiter) replace the head-time and cargo-token gap recorder above. `write_head_since()`, `head_since` on the waiter and the token arguments of the check are gone, with the tests that checked them.

**Files:**
- `scripts/lint/memory_admit.py` — the admission reader, with `queue_rows()` and `write_head_since()`
- `scripts/lint/memory_gate.sh` — cargo-token left and back times
- `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_build_queue.py`, `scripts/build_queue/test_queue_evidence.py` — both sides' tests
- `docs/as-built/build-memory-admission.md`, `docs/as-built/unit-board.md` — the merged as-built text

### Phase 1 — The build report compares build rates before and after each build moment  · status: done
**Depends on:** Phase 0

#### As-built

- `scripts/buildlog/moments.py` is the one place for build moments and the hana set:
  - `MOMENTS` holds 6 fixed moments (herdr cgroup move, baloo off, hana opt-level 1, hana Bevy dylib, the floor sweep, Build rules 1); Phase 3 adds a seventh. Moment 1 carries sdlc's week baseline; the floor sweep carries sdlc's clone repeat figures. Moment 2 is one new `MOMENTS` entry when its hash lands.
  - `HANA_CTE` is the one hana set: sdlc's `nat` predicate, with sessions and calls classified over each measured window, natedev only.
  - `MetricKey` names each metric; `RATE_ROWS` lists the rate rows.
  - `window_rates(connection, start, end, *, phases=())` makes one SQL population read and one Python pass for every row. A step with a NULL session or cwd counts in the totals but never pairs.
  - `phase_events()` reads `unit_runs.history_root()/runs`, natedev only.
  - `comparison_windows(moments, end)` is the one window rule: before = 7 days, cut at the previous moment; after = up to the next moment or the report end. A fixed moment shows while its after window reaches into the last 7 days. Observed profile changes keep the 7-day rule.
- `report.py`: `moments_section()` prints `### Before and after` and replaces the Power profile section in `buildlog report`. `report_end(day, now)` = min(now, day end), and `ci_freshness` uses it too. Each moment keeps the p50 table and adds a rates table. sdlc's figure prints beside ours when the two differ by more than 5%. The definitions and notes print once.
- `cli.py`: `buildlog moments [YYYY-MM-DD]` prints only that section, read-only, in about 2-4 s.
- Over sdlc's window 2026-10-24T10:40:20Z..2026-10-31T10:33:06Z the live numbers match the recheck within 1%: hana wait 1328.75 h, clippy 160.5/day, 1723 nextest / 1123 clippy steps, reruns 16.19%, queue-hidden 45.73%, after a pass 218 runs / 33.72 h.

**Files:**
- `scripts/buildlog/moments.py` — moments, hana set, rate rows, windows, phase events
- `scripts/buildlog/report.py` — `moments_section`, `report_end`
- `scripts/buildlog/cli.py` — `buildlog moments`
- `scripts/buildlog/test_moments.py` — fixture tests for moments, the hana set, rates and windows
- `scripts/buildlog/test_report.py` — the Before and after section in the report

**Ruled out:** a one-line moment heading — it would change 4 existing assertions.

### Phase 2 — The build report shows memory figures before and after the build budget raise  · status: done
**Depends on:** Phase 1

#### As-built

- `scripts/buildlog/moments.py` puts each moment on a track (`MomentTrack.BUILD` or `MomentTrack.MEMORY`, a class variable of the type, not a flag):
  - `MemoryMoment(at, name)` is a live memory moment. The live entry is `MemoryMoment(2026-10-31 13:33 UTC, "Build budget 21 → 24 GiB (e791376e)")`, 09:33 EDT.
  - `PendingMemoryMoment(name)` is a memory moment with no live time. The report prints `<name>: not live yet` in place of its table.
  - `comparison_windows` is still the one window rule, applied once per track. A memory moment never cuts a build moment's windows, and a build moment never cuts a memory moment's windows.
- `scripts/buildlog/memory.py`: `earlyoom_kills(start, end)` returns a typed list of `EarlyoomKill` (time, pid, command, `oom_score_adj`, cmdline) or `JournalUnavailable` (the Mac, or journalctl fails). A kill is a build kill when its `oom_score_adj` equals `build_oom_score_adj`, or when its cmdline program is under `/target/` (outside the build gate). The program is the cmdline text before the first space. `window_report` uses the same reader, and its output is unchanged.
- `scripts/buildlog/report.py`: a memory moment prints its own table (Before, After, Change) with these rows: earlyoom kills/day for builds as `N (M outside the build gate)`, earlyoom kills/day for others, full memory stall %, memory wait p90, and memory-kill retries/day with the stopped calls in brackets. An unreadable journal prints `unavailable` for the kill rows. Under the table, one overlap line names each build moment inside its windows, with date and time. The first memory moment's before column reads `no earlier memory moment`. `MIN_RATE_WINDOW_DAYS` is the one thin-window limit for the rate and memory tables. `buildlog moments` and `buildlog report` both show the memory moment.
- `config/lint.conf` holds `build_oom_score_adj=500` under `[build]`, the one build kill level:
  - `invoke.sh` reads it once, when it sources `lint_config.sh` (shell builtins only, no new process, 0.9 ms per source). A `declare -F` guard keeps `invoke.sh` working with an older reader that lacks the function.
  - Python reads it through `scripts/lint/config_values.py` (`build_oom_score_adj()`).
  - An absent key gives 500 with no message. A present invalid value prints a warning and gives 500.
- Live on natedev, the week before Build rules 1 reads 54 builds kills (9 outside the build gate) and 0 others. `buildlog moments` runs in 3.0–4.1 s. The buildlog suites (220 tests), the invoke scope suite (22 tests) and the 124 test files that reach `invoke.sh` pass.

**Files:**
- `scripts/buildlog/moments.py` — `MomentTrack`, `MemoryMoment`, `PendingMemoryMoment`, the per-track window rule
- `scripts/buildlog/memory.py` — `earlyoom_kills`, `EarlyoomKill`, `BUILD_OOM_SCORE_ADJ`
- `scripts/buildlog/report.py` — the memory moment table, overlap lines, `MIN_RATE_WINDOW_DAYS`
- `config/lint.conf` — `build_oom_score_adj` under `[build]`
- `scripts/lint/lint_config.sh`, `scripts/lint/invoke.sh` — the shell reader and the build scope's kill level
- `scripts/lint/config_values.py` — the Python reader
- `scripts/buildlog/test_moments.py`, `scripts/buildlog/test_report.py`, `scripts/buildlog/test_memory_window.py`, `scripts/lint/test_invoke_scope.py` — tests

**Gotchas:** earlyoom joins argv with spaces and no shell quotes, so only the text before the first space is the program. A journal that cannot be read is `unavailable`, never zero.

### Phase 3 — The build report splits build rates at the queue test-phase charge  · status: done
**Depends on:** Phase 1

#### As-built

- `MOMENTS` in `scripts/buildlog/moments.py` gains `BuildMoment(2026-10-31 20:36 UTC, "Queue test-phase charge (2a819dfd)", "all")`, 16:36 EDT, with no sdlc baseline. The showrunner set it when queue's test-phase charge and one decision pass reached main.
- It is not Moment 2. Moment 2 stays phases-unit's prompt and lint change.
- `buildlog moments` shows it as the newest section, with Build rules 1 (Moment 1) as its before window. Its after window is thin until a day of data exists.

**Files:** `scripts/buildlog/moments.py`; `scripts/buildlog/test_moments.py` (the pinned moment order).

### Phase 4 — /build:report names the hourly memory table  · status: done
**Depends on:** Phase 1

#### As-built

- `commands/build/report.md` names `buildlog usage-hours [YYYY-MM-DD]` near its top. The line lists what the table holds: hourly build memory, process memory, file cache, build and CI CPU, disk traffic, lowest free disk and free pool tokens.
- A second line tells the agent to run it for questions about memory, CPU or disk by hour, and to answer from its table in a few lines.
- Source: the user's change review C682 (2026-11-02). The table is hard to read in the CLI; agents use it for analysis.

**Files:** `commands/build/report.md`.

### Phase 5 — The build report compares moments per phase, not per day  · status: done
**Depends on:** Phase 1

2026-11-03 (the user, through the builds showrunner): the machines ran much lighter after Build rules 1. A time comparison per day does not show whether matters improved. The report needs a comparison per a unit of work.

#### As-built

- The unit of work is the finished natedev hana phase (`WindowRates.phase_count`).
- Each build moment's rate table leads with rows per phase: nextest, clippy, run hours, wait hours, failed nextest, nextest reruns, clone rebuilds, clone rebuild hours and fail-then-fail runs.
- One row shows hana wait hours per hana run hour. It is unavailable when run hours are zero.
- The per-day rows follow. Each cell suffix states its unit (`/phase`, `h /phase`, `/day`, `h /day`, `%`, `h wait / h run`). The header is `| Rate | Before | After | Change |`.
- Under the table: `Hana phases finished: before N, after M.` and a note. The note says per-day rows follow load, per-phase counts do not, and per-phase hours still fall on a quieter machine.
- `RateRow.formula` is `PerDay | Ratio`. One loop over `RATE_ROWS` computes every rate. Units `runs_per_phase` and `hours_per_phase` replace `per_phase`.
- Both moment tables and the Moment 1 baseline table use `table()`, `rate_change()` and `rate_window_days()`. The baseline line reads `Hana phases finished in the baseline: N.`
- Not added: rates per landed commit. A move of the note to one place per report is not made; the note stays under each table.

**Files:** `scripts/buildlog/moments.py`, `scripts/buildlog/report.py`, `scripts/buildlog/test_moments.py`.

### Phase 6 — Dylib A/B timing for hana  · status: todo
**Depends on:** the user lifts the hana pause (paused 2026-11-04).
**Workers:** 1 writer (the benchmark runner).

2026-11-03 (the user approved, through the builds showrunner): real-use data cannot isolate the Bevy dylib's effect. Time the same hana commit on the same machine with the dylib on and off. Run each side through verify.sh:

1. Run one cold build.
2. Run three incremental test runs.

The result goes to the showrunner. No repository code changes.

Method (side A and side B 1 already ran this way; keep it):
1. Use one shared clone of hana origin/main 72d2c08da9bdbc295f7a36af216e01941b582371. The source checkout stays unchanged.
2. Side A keeps `[workspace.metadata.dev] features = ["hana/dynamic_linking"]` in the root manifest. Side B removes only that activation.
3. Side A builds into `dylib_ab/target-a`, side B into `dylib_ab/target-b`, through `CARGO_TARGET_DIR`. Cold means an empty target folder; compiler and download caches stay.
4. Run each verify.sh call with `--local`, `--no-cache` and the measurement reason, on natedev only.
5. Record wall, build, test, wait and load per run. Exclude an interrupted run.
6. Each incremental run alternates adding and removing one comment in `crates/hana/src/environment/constants.rs`, a leaf module of the hana binary. Both sides use the same edit sequence; side B starts from the original file.
7. The completed runs are in `report-unit-dylib-ab.md` at the worktree root. Read it first and run only the missing runs.
8. The missing runs are side B incremental tests 2 and 3. The B2 run `20261103T221339-813897-4c39` ended with exit 143 while it waited for memory, so it is not a sample. Its record and log are in `/tmp/claude/unit/b090f8f1-cf8e-4d44-8e52-b223d94de9cc/mesh_runs/`. Check that record again before you select the runs.
9. The clone at `/tmp/claude/unit/b090f8f1-cf8e-4d44-8e52-b223d94de9cc/dylib_ab/hana` no longer exists (checked 2026-11-09). `target-a` and `target-b` remain beside it. Make a new clone at the same commit in that folder. If the target folders are gone, the side B runs need a new cold build first.

Completion gate: add one row to `docs/plans/builds-report-rounds.md` with the hana commit, the settings of each side, the measured times, the limits and the conclusion. Mark it as a controlled A/B result, not a phase-time measurement over ten hana phases. Record the `record.py` commit of each run: Phase 16 cut its CPU per step. If runs used different versions, say so; do not repeat completed runs for that reason.

### Phase 7 — The build improvement loop ranks costs and records each round  · status: done
**Depends on:** none (runs beside Phase 6)

#### As-built

- `buildlog costs [--days N]` (`scripts/buildlog/costs.py`) prints one ranked list of build hours lost by machine and cause, as h/day and h/finished phase. Causes: memory wait, cold rebuilds (50+ crates), failed runs, reruns after a pass, cancelled CI, and Mac idle while natedev waits. It runs in 0.45–0.70 s.
- The machine comes from `steps.target`, else from `record.target_hosts()`. No host list is copied.
- Mac idle = candidate waits minus Mac execution and complete Mac holds. Holds with no recorded end make the row an upper bound, with its unknown part beside it (`MacIdleEstimate`, `RankedCost.unknown_seconds`).
- Phase time reads only `phase_finished` events (wall time = finish minus `phase_started_at`). It prints the median and p90 for hana phases and for all phases.
- `moments.py` holds the shared `window_bounds`, `nextest_outcome`, `previous_nextest` and `nearest_rank`. `number_option.py` parses `--days` and `--hours`. `holds.py` and `mac_outcome.py` import coordination and recorder modules only where a reader needs them.
- `docs/plans/builds-report-rounds.md` holds the round record, with four round-1 rows.
- `/build:report` (`commands/build/report.md`) is retired. `buildlog report`, the index and every collector remain.

**Files:** `scripts/buildlog/costs.py`, `scripts/buildlog/number_option.py`, `scripts/buildlog/cli.py`, `scripts/buildlog/moments.py`, `scripts/buildlog/report.py`, `scripts/buildlog/holds.py`, `scripts/buildlog/mac_report.py`, `scripts/buildlog/mac_outcome.py`, `scripts/buildlog/test_costs.py`, `scripts/buildlog/test_moments.py`, `scripts/buildlog/test_report.py`, `docs/plans/builds-report-rounds.md`.

### Phase 8 — Build history as charts  · status: done
**Depends on:** Phase 7

The user, 2026-11-03: tables do not work for them. Lower priority. Design a chart view of the build history (costs and phase time over time, each build change marked). Design first; the build waits for the user's go.

#### As-built

- Design: `docs/plans/builds-report-charts-design.md`. The user said "go" on 2026-11-03, relayed by builds.
- `buildlog charts [--days 7|30] [--no-open]` (`scripts/buildlog/charts.py`) writes `~/.cache/buildlog/charts/latest.html`, publishes it with `review_link.py` and prints the tailnet link (Phase 22). The page is inline SVG with no server or external library. It has light, dark and Auto themes and daily buckets in the user's zone.
- Charts: hana phase time (daily median and p90), costs by cause as small multiples, and costs by machine. Each has numbered moment lines, a crosshair tooltip and a table view.
- `costs.py` is the shared cost reader for `buildlog costs` and `buildlog charts`. It pairs reruns and Mac runs in one pass, then splits at bucket edges. A missing applicable memory wait stays `CostUnavailable`, never 0. Mac idle is classified by coverage intervals; its unknown part is shown.
- `index.py` `COST_INDEXES` (made in `create_schema` and `open_for_update`) give indexed window reads instead of whole-table scans.
- Speed, as process CPU time (user+sys): 7 days 0.60–0.71 s (limit 1.5 s); 30 days 1.76–1.82 s (limit 3 s).
- The fresh design check found two label defects; Phase 10 fixes them.

**Files:** `scripts/buildlog/charts.py`, `scripts/buildlog/costs.py`, `scripts/buildlog/index.py`, `scripts/buildlog/cli.py`, `scripts/buildlog/test_charts.py`, `scripts/buildlog/test_costs.py`, `scripts/buildlog/test_index.py`, `docs/plans/builds-report-charts-design.md`.

### Phase 9 — Round 1: find why natedev rebuilds cold  · status: done
**Depends on:** Phase 7

Cold rebuilds (50+ crates) cost natedev 0.32 h per finished phase, the second largest cost. Find the cause before a fix. Query the index for cold steps: worktree, target folder, and the event before each (new clone, branch switch, toolchain or flag change, sweep deletion). Write the cause with counts and hours to `docs/plans/builds-report-rounds.md`, then send it to the showrunner, which names the owner. No code changes.

### Phase 10 — Chart labels do not collide  · status: done
**Depends on:** Phase 8

#### As-built

- Moment numbers sit in their own rows below each panel title. Numbers closer than 26 px go to the next row, and the plot top moves down to make room.
- End labels stay at their own line ends. Ends with the same bucket and value share one label, which names each series and the value.
- A label that does not fit, or that sits on another series' end, is dropped; the legend names that series. Gap notes yield to end labels.
- Coincident line segments alternate dashed strokes. A point shared by several series shows one sector per series.
- `test_charts.py` pins both rules: no moment number box overlaps a panel title box, and each end label is nearest its own line end.

**Files:** `scripts/buildlog/charts.py`, `scripts/buildlog/test_charts.py`.

### Phase 11 — Every build step records its real target folder  · status: done
**Depends on:** none (runs beside Phase 10)

#### As-built

- Cause (Phase 9): 1,399 of 1,517 cold natedev steps in 7 days had no absolute target folder (678 `unknown`, 721 unset).
- `scripts/buildlog/step_context.py` gives the shell each step's tree key and target folder in 0.08 s. It applies the command's `env` wrapper (options, `--`, `-u`, `-C`, assignments), Cargo config and workspace default. It does not start `cargo locate-project`. It works as a script, as a package module and with `python3 -m`.
- Caller metadata and known-folder hints are used only when the command does not change `CARGO_TARGET_DIR`, `CARGO_BUILD_TARGET_DIR`, `CARGO_HOME`, `HOME` or the working folder.
- Target states: an absolute folder, `none` (not a Cargo step), or `unresolved` (the folder is not known). A missing captured folder records `unresolved`; the detached writer never resolves late. Old `unknown` and `unset` rows stay as they are.
- `index.py` schema 29 stores `build_history_group` once: `folder:<path>`, `legacy:<checkout>` for old unknown/unset rows only, or NULL. NULL rows never read or make warm history. Index `steps_build_group_history` replaces `steps_build_history`. `report.py` compares the stored column.
- `scripts/lib/cargo_target_config.py` is the one Cargo config and workspace reader for the recorder and `scripts/lint/sweep.py` (`CargoConfigPolicy`).
- `scripts/lint/invoke.sh` writes each step through one `buildlog_write_step` function. verify-unit has the new line ranges.
- The live index check runs after the merge (Change-Review line in the checkpoint).

**Files:** `scripts/buildlog/step_context.py`, `parse.py`, `record.py`, `index.py`, `report.py`, `launches.py`, `scripts/lib/cargo_target_config.py`, `scripts/lib/shell_test_support.py`, `scripts/lint/invoke.sh`, `scripts/lint/sweep.py`, `scripts/direct/verify.sh`, and their tests.

### Phase 12 — A vault stop that waits on the user does not fail buildlog.service  · status: done
**Depends on:** none (urgent bump from the builds showrunner, 2026-11-03)

The user unit `buildlog.service` on natedev failed from 21:29 EST. The vault step stopped on uncommitted files in `~/rust/hanadocs` on the Mac and returned exit 1 (`vault_pull.py:310` returns 1 for every stop except "Mac unreachable"). The stop already alerts the user to commit or discard the files.

1. A vault stop that waits on a user action, with its alert sent, exits 0 and keeps its printed stop line.
2. A real failure (`command_failed`) still exits 1.

Acceptance: tests pin exit 0 for `tracked_files_changed` and exit 1 for `command_failed`. The next hourly `buildlog.service` run ends without failure.

**Files:** `scripts/buildlog/vault_pull.py`, `scripts/buildlog/test_vault_pull.py`.

### Phase 13 — Buildlog tests import their own modules  · status: done

Bump for builds: the bump gate on main be390f93c runs without this unit's PYTHONPATH. test_costs subprocess tests fail with no module cli or costs; test_vault_pull fails to import conf. Each test sets its own import path.

### Phase 14 — Build history charts open and show every machine's costs  · status: done
**Depends on:** Phase 8, Phase 10

#### As-built

- Cause (C950, C951 "not works", 2026-11-04): the aws-build target was configured with no build log, and "All machines" needed every configured machine, so every cost chart was unavailable. `buildlog` is also not on the terminal PATH; that fix is in `/etc/nixos` and goes through the builds showrunner.
- `costs.first_source_times` gives each machine's first indexed event. Empty and eventless monthly files do not count. `CostReading.first_sources` carries the mapping; `build_series` joins it with observations.
- `cost_value` returns `NoDataYet` for each day before a machine's first event. JS sums (All machines and grouped "Other") skip those cells. The chart, table, tooltip and detail line read the same cell. A machine with no data in the window shows one line, and the selector names it "no data yet".
- A machine with recorded data that misses a day still makes the day unavailable. A missing applicable wait stays unavailable.
- Page CPU: 7 days about 0.6 s, 30 days about 1.8 s.
- The checkpoint's Change-Review rows re-check C950 and C951 with the full path.

**Files:** `scripts/buildlog/charts.py`, `scripts/buildlog/costs.py`, `scripts/buildlog/test_charts.py`, `scripts/buildlog/test_costs.py`.

### Phase 15 — The build page gets its chart and top-cost data from one typed function  · status: done
**Depends on:** Phase 14

The user, 2026-11-04, change review C948 (`buildlog costs`), through builds: the top 3 costs move onto the one build page, each with the action that cuts it and its owner, beside the build history charts (C950). queue-unit's Phase 10m draws the page; this phase gives it typed data, no HTML. The top 3 rank the last 2 days, so a fixed cost does not stay on top.

#### As-built

- `charts.page_data(days: int, n: int = 3) -> PageData | IndexUnavailable` gives queue's build page feed its data. It opens the index once (`read_index`), reads costs once over the page window in one snapshot (`read_snapshot`), and builds the charts and the top costs from that one reading. `page_data(7)` takes 0.42-0.48 s wall on the live index.
- `PageData(charts: ChartSet, top: TopCosts)`. `TopCosts(start, end, recent_days, rows: tuple[TopCost, ...] | NoDataYet)`: `start` and `end` bound the recent window, in the user zone. `TopCost(machine, cause, recent: ChartCost, window: WindowValue, action: CostAction)`. `CostAction(action, owner)`.
- `WindowValue = WindowMatchesRecent | WindowDiffers(value: ChartCost)`. An unknown recent value is a match. A known recent value with an unknown window value is `WindowDiffers(window)`. Otherwise per-day rates differ when the gap is above 0.5 of the recent rate. An upper bound keeps its unknown part per day.
- Rows rank the six causes (`COST_KEYS`) by recent seconds and keep `CostUnavailable`, `NoDataYet` and `MacIdleUpperBound` as they are.
- `config/buildlog.conf` holds `TOP_COST_RECENT_DAYS=2` and `COST_ACTION_*`/`COST_OWNER_*` for each cause, read at use. `TOP_COST_RECENT_DAYS` greater than `days` raises `conf.Unusable` that names the key and the days.
- `buildlog charts` output is unchanged; `main` and `page_data` share `read_index`, and `collect_charts` and `page_data` share `read_snapshot` and `chart_series`.

**Files:**
- `scripts/buildlog/charts.py` — `page_data`, its types, the snapshot and index helpers.
- `scripts/buildlog/test_charts.py` — `PageDataTests`.
- `config/buildlog.conf` — recent days, actions and owners.

**Binds later work:** queue's build page feed calls `charts.page_data`; it stays when `buildlog charts` moves to the shared page.

**Ruled out:** a `read_costs` keyword that skips phase rows (one read makes it unnecessary); a snapshot inside `read_costs` for every reader (`buildlog costs` is its only unsnapshotted reader and goes away with the shared page).

### Phase 16 — Each build step spends less CPU to write its build-log row  · status: done
**Depends on:** none (from next items; showrunner disposition 2026-10-30: next)

#### As-built

- `record.py` loads config, target, hash, gzip, json, store and url parsing only on the paths that use them. The pending writer uses `os.open(O_EXCL, 0o600)` and `os.replace`; ids come from `os.urandom`.
- One owner for each rule: `repository.remote_identity` (repository key), `repository.repository_key` (origin remote, else `git-root:<first root>`; admission's `git_identity` calls it), `parse.test_threads` (admission keeps the name `memory_admit.test_threads`), `record._target_hosts` (one `sync_config()` read for all targets).
- `remote_identity` has an SCP shortcut that skips `urllib.parse`; it builds the path as the urlsplit path does, so keys are unchanged. `test_step_row.py` pins the keys in a table and uses a temporary build log.
- CPU ms per call, bevy_brp with a real Cargo.toml, before -> after (merged tree 08397569e): terminal 294.8 -> 150.3, tee 289.3 -> 151.4, call 118.2 -> 86.3, step-context 77.2 -> 60.0, test-phase-context 208.2 -> 80.9. The driver is in the phase 18 folder (`cpu_driver.py`).

**Files:** `scripts/buildlog/record.py`, `parse.py`, `step_context.py`, `costs.py`, `test_step_row.py`, `test_costs.py`, `scripts/lint/memory_admit.py`, `test_memory_admit.py`, three as-built docs.

**Gotchas:** `memory_admit.py` is edited by several units; the builds merge took builds' direct buildlog imports over phase 16's typed stubs.

### Phase 17 — The build log stores each memory reading once each minute  · status: done
**Depends on:** none (from next items; showrunner disposition 2026-10-30: next)

#### As-built

- Each `buildlog sample` tick writes the slices once, in the sample row. The tick `memory_snapshot` row (`TickMemorySnapshot`) holds `kind`, `at`, `host` and `zram` only. `memory.write_snapshot(at, host) -> None` writes it.
- The standalone `buildlog snapshot` row (`MemorySnapshot`) keeps its slices, because no sample row is beside it.
- `memory.nearest_snapshot` takes the nearest non-null slices from `samples` UNION ALL `memory_snapshots` (snapshot row first on equal distance), and the nearest zram in a second query. Old logs, new ticks and standalone snapshots give the same report. No slice reading in the window reads as unavailable, never as absent.
- Log size: 8,321,760 bytes/day before, 4,808,160 after (1,440 ticks).

**Files:** `scripts/buildlog/memory.py`, `scripts/buildlog/cli.py`, `scripts/buildlog/test_sample.py`, `scripts/buildlog/test_memory_window.py`, `docs/as-built/buildlog-memory-stalls.md`, `docs/as-built/build-memory-admission.md`.

**Gotchas:** old logs keep slices in `memory_snapshots`, so the reader reads both tables.

**Ruled out:** a join on equal `at` text between the two writers (a miss showed the slice as absent).

### Phase 18 — Each test step's row records the memory charge admission gave its test phase  · status: done
**Depends on:** none (from next items; showrunner 2026-11-04: natest P10 is on builds, the index.py freeze has ended, start now)

#### As-built

- Admission selects the charge after the grant and publishes it in its history row (`admission/anon_peaks.jsonl`, `test_phase_charge_use`) at release, before the recorder runs. The grant never carries it; `test-phase-context` writes `test_phase_charge: null` and keeps the sidecar's `reservation_id`.
- At step end and on pending-record retry, `record.completed_test_phase_charge(reservation_id)` searches the history from the end (mmap, shared flock, whole rows only), only when the sidecar's window is `ObservedTestPhase`. A match builds `ChargedTestPhaseRecord`; the row copies the evidence unchanged.
- Bad or unreadable evidence records null and writes one `errors.log` line; the row is always published. A step whose admission published no history row (no sampled peak, or unscoped) records null.
- The index has `steps.test_phase_charge_bytes` (`SCHEMA_VERSION` 32); older rows read null.
- `TestPhaseChargeUse`, `NoTestPhaseChargeUse`, `CallRecord`, `NoCallRecord` and `parse_test_phase_charge_use` live in `scripts/lib/test_phase.py`; `memory_admit` re-exports the names. `record.py` loads no `memory_admit`.
- CPU ms per call, bevy_brp: terminal 118.5 (charged with the real 11 MB history 117.0, no row 113.6), tee 112.1 (120.2, 117.0), call 56.3, step-context 50.5, test-phase-context 51.0. Drivers: `cpu_driver.py` and `cpu_driver_charged.py` in the phase 18 folder.

**Files:** `scripts/buildlog/record.py`, `index.py`, `test_step_row.py`, `test_index.py`, `scripts/lib/test_phase.py`, `scripts/lint/memory_admit.py`, `test_memory_admit.py`, `docs/as-built/buildlog-memory-stalls.md`.

**Ruled out:** reading the charge from the grant's sampling context (the charge is chosen later); a history offset in the sidecar (a search with no match costs about 1 ms).

### Phase 19 — Each step row names the output folders its own command built  · status: done
**Depends on:** none

#### As-built

- At step end, `step_context.step_output_roots(argv, cwd)` runs `cargo metadata` with the step's own Cargo inputs: the program (with `+toolchain`), the env wrapper's environment and folder, `--manifest-path`, `--config`, and the first `--target-dir` passed as `--config build.target-dir="<absolute>"`. `step_context.cargo_command_context` gives this split to both start capture and end read.
- When `target_dir` is an absolute folder and the read `target_directory` differs from it, the row has no pair and `errors.log` gets one line. With `target_dir` `unresolved` or `none`, a successful read still writes the pair. Old rows are unchanged.
- `cargo_target_config.cargo_folder_settings` applies Cargo's order under both policies, lowest first: config files, `CARGO_BUILD_TARGET_DIR`/`CARGO_BUILD_BUILD_DIR`, CLI `--config` (inline and file), `CARGO_TARGET_DIR`, `--target-dir`. Strict capture now reads `--config` (it ignored it since Phase 11); bad TOML or an unreadable file records `unresolved`. sweep.py gets the same order.
- `read_cargo_output_roots(checkout, *, cargo, options, environment)` builds `[*cargo, "metadata", "--format-version", "1", "--no-deps", *options]`; its six other callers keep the default call.
- CPU ms per call, bevy_brp, interleaved A/B against Phase 18 (f88894adc) on one loaded machine: terminal 150.5 → 152.3 (1.012x), tee 157.9 → 159.6 (1.010x). Driver: `cpu_ab.py` in the phase 19 folder.

**Files:** `scripts/buildlog/record.py`, `step_context.py`, `test_record.py`, `test_step_row.py`, `scripts/lib/cargo_output.py`, `cargo_output_test_support.py`, `cargo_target_config.py`, `test_cargo_output.py`, `docs/as-built/build-targets.md`.

**Gotchas:** Cargo reads `CARGO_TARGET_DIR` before all build config, CLI `--config` included; `CARGO_BUILD_TARGET_DIR` is ordinary config and loses to `--config`. A CPU baseline from an earlier phase is not valid under a different load: measure A/B in one run.

### Phase 20 — memory.py reads recorded output folders through one reader  · status: done
**Depends on:** resources' `build_shape.recorded_output_roots` on builds.

#### As-built

- `memory.build_output_roots(index_file, current_checkout, host)` reads recorded Cargo pairs through `build_shape.recorded_output_roots(index_file, host)`, the reader the floor sweep uses. One reader of the steps output pairs remains. The signature, `@lru_cache(maxsize=1)` and `build_output_program` are unchanged.
- Each `AbsoluteRecordedRoot` side of a pair adds its resolved path; each distinct path resolves once. Each `AbsoluteRecordedRoot` workspace key adds a checkout beside the current checkout. `RecordedRootsUnavailable` leaves only the current checkout. Rows from another host and rows with a `--target-dir` override add nothing (the reader's rule).
- The `cargo metadata` fallback is unchanged: each checkout with a `Cargo.toml` gets `read_cargo_output_roots`; only a `CargoOutputRoots` result adds roots.
- The import of `build_shape` is lazy, inside the function, with a `TYPE_CHECKING` split (`from ..lint import build_shape` for basedpyright, `from scripts.lint import build_shape` at run time), so `buildlog snapshot` does not load it.
- On the live natedev index the root set is 157 (was 166): 6 lost roots lie under kept roots and 3 come from old `unknown`-pair vendor/bevy_render rows whose folders do not exist, so the kill classification is the same.
- First-call own CPU is about 420-450 ms (was about 120 ms), with `cargo metadata` stubbed; accepted for one reader. Wall time stays about 10 s from 116 sequential `cargo metadata` probes.

**Files:**
- `scripts/buildlog/memory.py` — `build_output_roots` calls the shared reader.
- `scripts/buildlog/test_memory_window.py` — `BuildOutputRootsTests`: six outcome tests with a `build_roots(outputs, index_file)` helper; fixtures use `create_step_index` and `record_root_attempt` from `scripts/lint/test_build_shape.py`.

**Binds later work:** the parallel probe change keeps the reader call, the lazy import and the `build_roots` test helper. Resources' later reader change (dedupe pairs, row facts, move into buildlog) edits `memory.py` after this unit's merges.

**Gotchas:**
- The shared reader returns duplicate pairs because its `SELECT DISTINCT` includes argv: 15,838 pairs, 1,120 distinct on natedev.
- The tree's own index path can name a schema file that does not exist live; a timing run must pass the live index path.

**Ruled out:** an equal-root-set gate — a lost root is acceptable when a kept root covers it or its folder does not exist.

### Phase 21 — Each memory tick writes one row  · status: done

#### As-built

- Each `buildlog sample` tick writes one sample row, and that row carries `zram`. The tick writes no `memory_snapshot` row, and `cli.py` `sample` has no second writer.
- `memory.read_zram() -> ZramPresent | ZramAbsent` reads `BUILDLOG_ZRAM` (default `/sys/block/zram0`). `sample.sample()` (lazy import) and `memory.snapshot()` call it. `zram_state` reads an unreadable path as absent, so the sample is kept.
- `MemorySnapshot` is one TypedDict (`kind`, `at`, `host`, `zram`, `slices`): the standalone `buildlog snapshot` row.
- `index.py`: `samples.zram` TEXT column; `SCHEMA_VERSION = 33`.
- `memory.nearest_snapshot` uses one local `nearest_value(column)` over `samples` UNION ALL `memory_snapshots`, key (distance, snapshot row first, `at`). It takes the nearest slices, then the nearest zram. No slices in range reads as unavailable.
- CPU per tick 313.0 → 297.1 ms (interleaved A/B against c5d618d86); log size 4,808,160 → 4,698,720 bytes/day.

**Files:**
- `scripts/buildlog/sample.py` — zram in the sample record.
- `scripts/buildlog/index.py` — the `zram` column and schema 33.
- `scripts/buildlog/memory.py` — `read_zram`, `MemorySnapshot`, `nearest_snapshot`.
- `scripts/buildlog/cli.py` — the `sample` command writes one row.
- `scripts/buildlog/cgroup_test_support.py` — `write_zram` and `ZRAM_READING`.
- `scripts/buildlog/test_sample.py`, `test_memory_window.py`, `test_index.py`, `test_mac_idle.py` — tests.
- `docs/as-built/build-memory-admission.md` — owns "Slice snapshots" and the schema number; `docs/as-built/buildlog-memory-stalls.md` points to it.

**Gotchas:**
- Old logs keep zram and old slices in `memory_snapshots`, so the reader reads both tables.
- An equal-distance test needs different `at` text forms (`…00.000Z` sorts before `…00Z`), or text order alone picks the winner.

**Ruled out:** moving the zram reader to `cgroup_capture.py` — it removes no copy and gives no CPU gain.

### Phase 22 — buildlog charts prints a tailnet link  · status: done

#### As-built

- `buildlog charts` writes `~/.cache/buildlog/charts/latest.html`, publishes it with `review_link.snapshot`, and prints only the bare tailnet link. It prints no file path. Two runs print the same link.
- `charts.present_page(series, path, no_open) -> int` puts the page write and the snapshot under one handler of `review_link.SNAPSHOT_ERRORS`. A failure prints one stderr line (`buildlog charts: publication failed: <reason>`) and returns 1.
- `charts.local_display(environ, platform) -> bool`: false in any ssh session (`SSH_CONNECTION` set); otherwise true on darwin, or with `DISPLAY` or `WAYLAND_DISPLAY`. Only then does `webbrowser.open(link)` run, under `suppress(OSError, webbrowser.Error)`. `--no-open` and `BUILDLOG_CHARTS_NO_OPEN=1` skip the open.
- `review_link.SNAPSHOT_ERRORS = (OSError, ValueError, subprocess.SubprocessError)`; `review_link.main` and charts catch it.

**Files:**
- `scripts/buildlog/charts.py` — `present_page`, `local_display`.
- `scripts/buildlog/test_charts.py` — link, display table, browser failure, cache and review-root failure, stable-link tests; `check_publication` fixes the platform.
- `scripts/review_links/review_link.py` — `SNAPSHOT_ERRORS`.
- `scripts/buildlog/cli.py` — the charts help line.

**Gotchas:**
- `review_link.snapshot` costs about 141 ms CPU per call, mostly its `change_routing` import (natedev-efficiency owns that cost).
- Tests that reach `present_page` must fix `charts.sys.platform`: darwin with no environment is a local display.

**Ruled out:** opening the page on the Mac over ssh with a target config read — the user's rule is "it should always be a tailscale link".

### Phase 23 — The first kill lookup runs its cargo metadata probes in parallel  · status: done

#### As-built

- `memory.build_output_roots` runs `read_cargo_output_roots` for each checkout with a `Cargo.toml` through `ThreadPoolExecutor(max_workers=workers)` and `executor.map`. The root set does not depend on probe order, and a probe exception reaches the caller.
- `config/buildlog.conf` `METADATA_PROBE_WORKERS=8` (`# whole: 1..`) sets `workers`, read at use with `conf.read("buildlog").whole(...)`.
- On the live natedev index (157 roots, same set), the first kill lookup takes 1.08 s wall instead of 4.98 s. Total CPU is 5.54 s instead of 4.85 s, because parallel `cargo metadata` runs contend. The cost is paid once per report process that classifies a kill.

**Files:**
- `scripts/buildlog/memory.py` — the pooled probe loop; `concurrent.futures` is a top-level import, and `build_shape` stays a lazy import (import cycle).
- `config/buildlog.conf` — `METADATA_PROBE_WORKERS`.
- `scripts/buildlog/test_memory_window.py` — `probe`, `configure_workers` (`write_config_fixture`, so the committed limits stay) and `probes_meet` (one `threading.Barrier(2)`) helpers; tests for overlapping probes, one worker in sequence and exception propagation.

**Gotchas:** `METADATA_PROBE_WORKERS=1` restores the sequence and the lower CPU.

**Ruled out:**
- Checking recorded roots before the probes — a second cached reader for a path that runs about once a month.
- Pooling the `disk.py` timer probes — background wall time only; CPU stays the same.
- Probing fewer checkouts from recorded pairs or saved roots — the roots go wrong after a Cargo config change.
- A test that a fractional worker count is refused — it tests only the conf library, which `scripts/lib/test_conf.py` covers.

### Phase 24 — Mac offload reads the repository key without loading memory_admit  · status: done
**Depends on:** queue 10z on builds (done, b7ad7675c).

#### As-built

- `offload.repository_identity` reads `repository.repository_key(str(source.parent))`: the normalized origin URL, else `git-root:<root commit>`, else `store.UNKNOWN_IDENTITY`, which maps to `KnownRepository(name)` with no key. A lookup runs 1 git process in place of 3. Keys are unchanged; offload CPU per lookup drops by about 75 ms.
- `persist_attempt` writes `repository.key`, else `store.UNKNOWN_IDENTITY`. A keyless repository no longer writes an empty `repo_identity` that `index.py` reads as a real identity.
- `memory_admit.git_identity` reads the same function and keeps only its name and legacy-name rules, so one place holds the key rule. `git_output` is gone.
- `offload.py` imports `record` and `store` at module top; it calls no `memory_admit` function.
- `test_linux_runner` wraps its in-process child `exec` in `contextlib.chdir(os.getcwd())`, so the fake child cannot move the test process's working folder.

**Files:**
- `scripts/mac_test/offload.py` — repository key for claims, mirrors and attempts.
- `scripts/lint/memory_admit.py` — `git_identity` name rules over the shared key.
- `scripts/mac_test/test_target_slots.py` — `RepositoryKeyTests`: origin forms, root-commit key, no commits, unknown attempt, and a `sys.setprofile` check that a lookup runs no `memory_admit.py` code.
- `scripts/lint/test_memory_admit.py` — real-repository key and legacy-name cases.
- `scripts/mac_test/test_linux_runner.py` — working-folder restore.

**Gotchas:**
- Importing `offload` still loads `scripts.lint.memory_admit` through `sweep` -> `build_hold` -> the admission modules. resources-unit owns that chain.
- Check "no `memory_admit` code runs" by the files a call executes, not by `sys.modules` names: the chain loads `scripts.lint.memory_admit`, not the bare name.

**Ruled out:** passing the eligibility `KnownRepository` into `persist_attempt` (about 4 ms per offload run); deleting `git_reply` fixture branches that other `record` paths still reach. natest-unit moved the key to `repository.repository_key(directory)` with no `argv`; the merge of builds 5493dfa93 switched `offload.py` and `memory_admit.git_identity` to it.

### Phase 25 — A kill counts as a build when its program is under a Cargo target folder  · status: done

#### As-built

- `memory.build_output_program(program)` returns True when a resolved parent folder of an absolute program satisfies `sweep.is_cargo_target`. A relative program is never a build. `sweep` is imported lazily under the module name `sweep`, as in `disk.py`.
- `EarlyoomKill.is_build` is the configured `build_oom_score_adj` or a target program. `outside_build_gate` is a target program without that adjustment. One `_target_program` helper holds the folder check.
- No root cache, no index read and no `cargo metadata` probe run. The first kill lookup in a fresh process takes 0.09 s CPU and 0.09 s wall on natedev.
- A kill under a removed Cargo folder is not a build. On natedev, 5 of 6 logged kills count as build.
- The build report's kill definition line says "under a Cargo target folder".

**Files:**
- `scripts/buildlog/memory.py` — the classifier.
- `scripts/buildlog/test_memory_window.py` — `cargo_output(path)` fixture helper; `BuildOutputProgramTests`.
- `scripts/buildlog/test_moments.py` — moment tests use `cargo_output` folders.
- `scripts/buildlog/report.py` — kill definition line.
- `docs/as-built/buildlog-memory-stalls.md` — classifier paragraph.

**Gotchas:**
- `METADATA_PROBE_WORKERS` stays in `config/buildlog.conf` with no reader until the user answers the key deletion question.
- Tests that classify kills need a fixture folder with Cargo markers; a plain `target/debug` path is not a build.

**Ruled out:** a per-kill `cached_property` (under 1 ms per report).

### Phase 26 — buildlog costs is gone and buildlog charts opens the shared build page  · status: done

#### As-built

- `buildlog charts` publishes no page of its own. `charts.main` runs `scripts/build_queue/memory_feed.py link` (the `/build:page` command) from the same checkout and prints its one URL line. It opens the URL when a local display exists and `--no-open` is absent. `--days 7|30` stays.
- No link prints `buildlog charts: <reason>` to stderr and exits 1. No link means: a non-zero exit, a missing command, or stdout that is not one printable `http`/`https` URL with a valid port. The reason is the command's stderr when it gave one.
- `buildlog costs` is gone: the CLI prints its unknown-command usage and exits 2. `read_costs`, `aggregate_costs`, `collect_costs` and the cost types stay; the shared page reads them through `page_data`.
- `render_html`, `publish`, `PAGE`, `chart_data` and the cost text output are gone. Calculation tests read the typed `ChartSet`.
- The cost Definitions are sentences in `docs/as-built/buildlog-memory-stalls.md`. `build-report-session.md`, `build-targets.md` and `mac-test-offload.md` name `buildlog charts`.
- Live check, 2026-11-09, from this branch: `buildlog charts --no-open` printed `http://100.103.14.30:8765/h7uj5c/memory_live.html`, the same line as `/build:page`.

**Files:**
- `scripts/buildlog/cli.py`, `scripts/buildlog/costs.py`, `scripts/buildlog/charts.py`
- `scripts/buildlog/test_costs.py`, `scripts/buildlog/test_charts.py`
- `docs/as-built/build-report-session.md`, `docs/as-built/build-targets.md`, `docs/as-built/mac-test-offload.md`, `docs/as-built/buildlog-memory-stalls.md`

**Gotchas:**
- `~/.claude/scripts/buildlog/buildlog` runs the installed copy, not the worktree. Check a branch with `scripts/lib/py scripts/buildlog/cli.py charts --no-open` from the worktree.

### Phase 27 — A step whose Cargo folders are unknown records them as unknown  · status: done

#### As-built

A step row has the Cargo output pair from its record, or `unknown` in both folders. The index never builds the pair from `target_dir`.
- `index.add_step` reads `target_directory` and `build_directory` only from the record. When either one is absent, it stores `store.UNKNOWN_IDENTITY` in both. `target_dir`, `target_folder` and `build_history_group` still read `target_dir`.
- Column text: `target_directory` is "resolved Cargo artifact root; unknown when the record has no output pair"; `build_directory` is "resolved Cargo intermediate root; unknown when the record has no output pair".
- `SCHEMA_VERSION` is 35, so `index.update()` rebuilds every old step row once. Column text alone does not change the index file name, because `schema_statements()` leaves out the descriptions.
- Readers: `step_outputs.recorded_output_roots` gives an `UnknownRecordedRoot` for an unknown pair, and `sweep.discover_cargo_outputs` skips it.

**Files:**
- `scripts/buildlog/index.py` — `add_step` and the column text.
- `scripts/buildlog/test_index.py` — the pair table (legacy, override, target-only and build-only rows read unknown), the reader test through `recorded_output_roots` and `discover_cargo_outputs`, and the schema-bump rebuild test.

**Gotchas:**
- Records from before the pair and records whose Cargo metadata failed have the same fields (`v` is 1 for both). A fallback for one is a made-up pair for the other.
- On the live log (2026-11-08), 102,951 of 109,321 steps read unknown; the rebuild took 6.0 s.

**Ruled out:**
- A pair made from `target_dir` for old records: it is wrong when Cargo's build folder differs.

### Phase 28 — A verify.sh step takes its output folders from verify.sh's Cargo metadata  · status: done

#### As-built

- `verify.sh` `set_gate_metadata` runs `cargo metadata --no-deps --format-version 1`, sets `GATE_METADATA` and exports `BUILDLOG_OUTPUT_ROOTS` from `step_context.py output-roots` (one line: `<evidence>\t<workspace_root>\t<target_directory>\t<build_directory>`, or empty). `load_metadata` calls it only when `GATE_METADATA` is empty, so a reuse of the saved metadata keeps the export of the real read.
- `step_context.step_output_roots` reads the export first. When the step's `_output_evidence` equals the exported evidence, it returns the exported folders (resolved); else it reads `cargo metadata` as before. The user kept this folder-match check (2026-11-05).
- `_output_evidence` hashes the resolved working folder, the `+toolchain` word, each `--manifest-path`/`--config`/`--target-dir` argument as written, the Cargo folder environment values, and the content of `Cargo.toml`, `rust-toolchain(.toml)` in each parent and `config`/`config.toml` in each Cargo config folder.
- `cargo_output.cargo_metadata_output_roots(text)` is the one metadata decode; `cargo_target_config.cargo_config_folders(cwd, environment)` is the one Cargo config folder list.
- Recorder CPU per verify.sh step on bevy_brp: terminal 157 → 119 ms, tee 157 → 118 ms.

**Files:**
- `scripts/buildlog/step_context.py` — evidence, the `output-roots` verb, the export read in `step_output_roots`.
- `scripts/direct/verify.sh` — `set_gate_metadata` and the export.
- `scripts/lib/cargo_output.py`, `scripts/lib/cargo_target_config.py` — the shared decode and folder list.
- `scripts/buildlog/test_step_row.py` (`VerifyOutputEvidenceTests`), `scripts/direct/test_verify_python_starts.py`, `scripts/lib/test_cargo_output.py` — tests.

**Gotchas:**
- A step with `--manifest-path`, `--config` or `--target-dir` never matches the plain export and reads metadata.
- Suite tests use a fake `cargo` with fixed answers: `merge_gate.py` puts a Cargo tripwire first on `PATH`. A fake that execs the installed cargo wrapper loops unless it removes its own folder from `PATH`.

**Ruled out:**
- Reading the test-path metadata without an export, and an export through a `verify_metadata.py` request: routed to verify-unit, which owns `verify.sh`.

### Phase 29 — The Mac's hanadocs vault syncs both ways and asks a question only on a conflict  · status: done

#### As-built

`buildlog vault` syncs the Mac's `~/rust/hanadocs` with GitHub each hour, from natedev. A stop files one question on natedev's open-questions list and sends no phone push (user, 2026-11-05).
- natedev pushes its `refs/remotes/origin/main` to the Mac. The Mac script (`REMOTE_PULL`) stops with `branch` off main, and stops with the conflict outcome on an open merge (`MERGE_HEAD`: the unmerged files, else the files that differ from HEAD; no abort, add or commit; the user chose this check, 2026-11-05). It then runs `git add -A`, commits once (`vault: Mac edits`) and runs `git merge --no-edit --no-overwrite-ignore refs/remotes/origin/main`. A conflict prints the `--diff-filter=U` files and runs `git merge --abort`. The commit and the merge retry a busy `index.lock` (`vault.lock_retries`, `vault.lock_retry_delay_s`). The `VAULT_OUTCOME=` marker gives the outcome, because Mac login can hide the exit status.
- natedev fetches the Mac's main into `refs/remotes/mac/main`, then `origin main` after `ci.warm()`. It pushes a fast-forward, or a `git merge-tree --write-tree` plus `git commit-tree` merge made in the object store, to GitHub `main`. A natedev merge-tree conflict, cold credentials and a rejected push (`git push --porcelain`) are deferrals: one printed line, exit 0. The next hour's Mac merge files the conflict question.
- `mac_git` alone applies the Mac ssh options. A nonzero exit 255, or connection text on a nonzero exit, is `MacUnreachable`; a timeout is too.
- `PullOutcome = Pulled | Current | DeferredSync | PullStop`; `PullStop = VaultConflict | NotOnMain | MacUnreachable | VaultLocked | VaultCommandFailed`; `StopCause` is the `Literal` of `conflict`, `not_on_main`, `mac_unreachable`, `vault_locked`, `command_failed`.
- State (`store.root()/vault_pull.json`): `VaultState(last_good: LastGoodPull | NeverPulled, outage: OpenOutage | NoOutage)`, `OpenOutage(cause, since, asked, detail)`, written with `write_text_atomic` under `file_lock.exclusive`. An outage files one question with `update_registration.py ask --urgency soon --due <since + 1 day>`; `asked` is saved before `ask` and reset when `ask` fails. `MacUnreachable` files only after `UNREACHABLE_GRACE_S` (24 h) from `last_good`; a deferral after a Mac contact refreshes `last_good` and keeps the outage. A good run deletes the local outage; the question stays open until natedev clears it by hand.
- The `vault.stopped` push is gone from `push_catalog.py`, `push_texts_reviewed.json` and `test_push_texts.py`.

**Files:**
- `scripts/buildlog/vault_pull.py` — the job, the Mac script, the state and the question.
- `scripts/buildlog/test_vault_pull.py` — real-git tests (a GitHub bare repo, a natedev clone and a Mac clone, built once per class and copied for each test).
- `scripts/buildlog/test_target_sync.py` — the vault stand-in uses the marker and the Mac transport calls.
- `scripts/buildlog/cli.py` — the usage line.
- `config/build_targets.conf`, `config/README.md` — the lock retry text.
- `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json`, `scripts/notify/test_push_texts.py` — no `vault.stopped`.

**Gotchas:**
- A successful git transfer can print connection text on stderr (hooks). The unreachable rule applies only to a nonzero exit.
- The Mac needs no GitHub credentials: natedev carries the Mac's commits to GitHub.
- The first run committed the Mac's `home-base/vault map.base` change; that was the wanted result.

**Ruled out:**
- A phone push for a vault stop (user, 2026-11-05).
- A natedev question for a merge-tree conflict: no working tree holds natedev's merge.
- Importing `update_registration.py` internals: the job runs the public `ask` command.

### Phase 30 — The Mac idle watch sends one alert per idle episode  · status: done

#### As-built

One idle episode sends one showrunner message and at most one phone push. A changed cause or holder in the same episode sends one new message.
- `mac_idle.mac_work` reads `run.json` first, then `mac_test.read_claims(mac_test.StatePaths(paths.mac_state))`, filtered by `run_is_live`. The claims folder time no longer moves the episode start.
- `watch_once` ends the episode when the Mac runs natedev work, or when a live claim that started before the episode start has a `work_idle_since` later than that start. `mac_test` writes one activity answer for each host into every live claim, so this holds for every cause, blocked included, and another repository's claim does not restart the episode. No claim path is saved.
- The alert record saves `changed`, the time the current cause and holder started. `SavedAlert.changed: float` is required, and `read_alert` validates it like `since`; a record without it is unreadable, so the next pass starts clean.
- `outage_key(saved)` gives the message key `mac-idle-showrunner-<since>-<changed>`, so a cause that returns to an earlier value also sends. `messaged` is kept when the key does not change. The phone push stays once per episode (`mac-idle-<since>`).
- `cleared --since` and the push `--clears-when` are unchanged.

**Files:**
- `scripts/buildlog/mac_idle.py` — `mac_work`, `watch_once`, `outage_key`; `OutageMessage` carries `key`.
- `scripts/buildlog/mac_idle_alert.py` — `SavedAlert.changed`.
- `scripts/buildlog/test_mac_idle.py`, `scripts/buildlog/test_mac_idle_alert.py`, `scripts/buildlog/mac_idle_test_support.py` — the 12:50-13:09 repeat case, cause changes, a returning cause, a blocked episode that ends on Mac work, another repository's claim that sorts first, `test_own_claim_work_restarts_the_idle_interval`.

**Ruled out:**
- Saving the episode's claim path: it missed work in a blocked episode and took another repository's claim.
- A cause-and-holder message key: it did not send when a cause came back.

### Phase 31 — A vault sync that works again closes its open question by itself  · status: done

#### As-built

A good vault sync closes the question that its outage filed, through the public `withdraw` command. This replaces the earlier rule that natedev clears the question by hand.
- `question_text(since: float) -> str` in `vault_pull.py` is the one source of the short line: `Hanadocs vault sync stopped, <opened iso>`. The cause is only in `--question`, because a cause can change inside one outage and `withdraw` needs the exact text.
- `registration(command: str, *args: str) -> bool` runs `update_registration.py <command> --production <PRODUCTION> <args>` through `run(..., COMMAND_TIMEOUT_S)`. `ask_question(reason, choices, since)` files the text through it, and `withdraw_question(since) -> bool` runs `withdraw --item <text>` through it.
- In `_vault_locked`, a `Pulled` or `Current` outcome writes `VaultState(LastGoodPull(now), NoOutage())` first. It then withdraws once when the earlier outage is an `OpenOutage` with `asked=True`.
- A failed or timed-out withdraw still exits 0 and keeps `NoOutage`. The sync line ends `; the question was not withdrawn`, and nothing retries.
- A `DeferredSync` keeps the outage and withdraws nothing. An outage with `asked=False` withdraws nothing. `VaultState` and `vault_pull.json` keep their form.

**Files:**
- `scripts/buildlog/vault_pull.py` — `question_text`, `withdraw_question`, `ask_question`, `_vault_locked`.
- `scripts/buildlog/test_vault_pull.py` — `QUESTION_STAND_IN` keeps withdraw calls apart from ask calls; real-git tests for each case above.

**Gotchas:**
- A question the user already answered gives the same exit 2 as a real withdraw failure, so a rare failure leaves a stale question for natedev to clear.
- The live check waits for the next real outage on natedev, because a forced outage files a real question on the user's list.

**Ruled out:**
- Saving the filed text in the state file: `question_text` rebuilds it from `since`.
- A retry of a failed withdraw: an answered question would retry for ever.
- A withdraw on a deferral: a deferral is not a good sync.
- A migration: the live state had no outage and no open hanadocs question.

### Phase 32 — One shared helper merges overlapping time spans  · status: done

#### As-built

`scripts/lib/intervals.py` is the one place that merges overlapping time spans. All four callers use it and keep no merge loop of their own.
- `merged_spans(spans: Iterable[tuple[float, float]]) -> list[tuple[float, float]]` returns the spans sorted by start, with each overlapping or touching span merged into the one before it.
- `union_seconds(spans: Iterable[tuple[float, float]]) -> float` returns the duration that at least one span covers.
- `hold_duration` in `scripts/buildlog/holds.py` passes each span as whole microseconds from the requested start, and converts the result back through a `timedelta`. `MICROSECOND` is the unit.
- `source_coverage` in `scripts/buildlog/costs.py` groups spans by machine and builds its rows from `merged_spans`. The order stays machine, then start.
- `_elapsed_union` in `scripts/history/history.py` returns `union_seconds` and keeps its `started_at > 0` filter.
- `_work_seconds` in `scripts/direct/progress_history.py` walks `merged_spans` and keeps its idle grace.

**Files:**
- `scripts/lib/intervals.py` — `merged_spans`, `union_seconds`.
- `scripts/lib/test_intervals.py` — one table of six cases, and one generator test.
- `scripts/buildlog/holds.py` — `hold_duration`, `MICROSECOND`.
- `scripts/buildlog/test_holds.py` — fractional hold seconds total a full minute.
- `scripts/buildlog/costs.py` — `source_coverage`.
- `scripts/history/history.py` — `_elapsed_union`. Owner: sdlc.
- `scripts/direct/progress_history.py` — `_work_seconds`. Owner: sdlc.

**Gotchas:**
- Float timestamps lose precision: holds at second offsets [0,20.1], [30,50.1], [60,79.8] add up to 59.99999976158142 seconds and read `0:00`. Pass whole numbers to the helper when an exact total matters.
- `costs.py` and `vault_pull.py` get `scripts/lib` on their path through another module's import. The root fix is one shared path setup for `scripts/buildlog`.

**Ruled out:**
- A key argument on the helper: all callers fit plain float pairs.
- A `sys.path` line in `costs.py` and `vault_pull.py`: each adds one more copy of that line.

### Phase 33 — scripts/buildlog imports from the repo root  · status: done

#### As-built

All of `scripts/buildlog` and every outside importer of its modules use the repository's one import form. Behavior does not change.
- A module or test module imports a sibling as `from scripts.buildlog import y` or `from scripts.buildlog.y import z`, and another script folder as `from scripts.lib.y import z`.
- No module in `scripts/buildlog` has a `sys.path` line. The seven modules that run by path (`cli.py`, `record.py`, `mac_idle.py`, `step_context.py`, `health.py`, `port_lint.py`, `width_trial.py`) start with the shared `scripts/lib/package_root.py` bootstrap that devtools 3z added (merge of builds f7e3cbce3).
- A patch string names the root form: `mock.patch("scripts.buildlog.record.x")`. A code string that a test runs in a subprocess imports through the repo root.
- Outside `scripts/notify`, no file imports a `scripts/notify` module by a bare name.
- `_register_module_aliases` and `test_store_identity.py` do not exist. `scripts/buildlog/__init__.py` holds only a docstring; it adopts no bare module, and basedpyright needs it to resolve `scripts.buildlog` to this checkout.

**Files:**
- `scripts/buildlog/` — every module and test module, in the root form.
- `scripts/buildlog/test_repo_root_imports.py` — `test_entry_scripts_import_without_pythonpath` runs each by-path module with `PYTHONPATH` removed and an unrelated `scripts` package in the working folder; `test_entry_scripts_load_each_shared_module_once` checks that a shared module loads under one name.
- `scripts/aws_target/`, `scripts/build_hold/`, `scripts/build_queue/`, `scripts/build_targets/`, `scripts/lint/`, `scripts/mac_test/`, `scripts/test_times/` — outside importers of buildlog modules.
- `docs/as-built/build-followups.md`, `docs/as-built/build-targets.md` — the path line of `disk.py` and the rule on shared module names.

**Binds later work:** A buildlog import, patch string or test code string uses the root form. A library module adds no `sys.path` line.

**Gotchas:**
- A caller suite list misses test strings in modules outside the list; the merge gate finds them.
- The type check of the gate passes `--pythonpath` with a pinned venv, so that `bpy` and `PIL` resolve. Bare `basedpyright` differs from it.
- `merge_gate.py --tests-only` runs no type check unless `--type-dirs` names folders.
- Another unit can add a bare import of a buildlog module until it merges this phase.

**Ruled out:**
- A path module for `scripts/buildlog`: the repo already has one way, and a per-folder module is a second way.
- Moving `sessions` and `unit_runs` in this phase: other files import them bare in large numbers.

### Phase 34 — A failed merge gate module keeps its failure text  · status: done

#### As-built

A failed merge gate attempt keeps the end of its output in the build log, for local and cloud gates. A passing or restarted attempt keeps no log. Nothing prunes the logs; the per-file limit bounds the growth.
- `gate_record.Execution` has a required `output: str`, and `ModuleRun` has `log: str`: the kept log's path relative to the build log root, or empty.
- `gate_record.keep_failure_logs(record_id, host, epoch, modules, executions)` pairs modules with executions by `zip(..., strict=True)`. For each attempt with a non-zero `status` and an empty `restart`, it writes the last `GATE_FAILURE_LOG_KIB` KiB through `record.keep_log`, after one line that gives the count of cut bytes. The log name is the record id plus a sha256 of (suite, module, attempt). A failed write goes to `report_not_written`; the verdict and the record stay.
- `write_gate_run(run, executions)` takes its executions with no default and calls `keep_failure_logs`. `merge_gate.record_gate_run` passes each `job.output`.
- Cloud: `gate_box.execution_records(run)` builds the one `Execution` list, with output, from `run.executions or run.jobs`; `completion_record` uses it. `cloud_gate.import_record` decodes `row['gate']` and calls `keep_failure_logs` on natedev with the record's id, host and start epoch before it appends the box's record.
- `index.py`: `gate_modules.log` column, `SCHEMA_VERSION` 36. A record without `log` indexes the column as NULL.

**Files:**
- `scripts/test_times/gate_record.py` — `Execution.output`, `ModuleRun.log`, `keep_failure_logs`, `write_gate_run`.
- `scripts/test_times/gate_record_test_support.py` — `fixture_executions`, the one fixture that builds executions from module rows.
- `config/test_times.conf` — `GATE_FAILURE_LOG_KIB=64`.
- `scripts/production/merge_gate.py` — passes the output.
- `scripts/aws_target/gate_box.py`, `scripts/aws_target/cloud_gate.py` — the cloud list builder and the import that keeps the logs.
- `scripts/buildlog/index.py` — the `log` column.
- Tests: `test_gate_record.py`, `test_merge_gate_test_times.py`, `test_gate_index.py` (`test_query_recovers_failure_log_paths_after_an_index_rebuild`), `test_cloud_gate.py` (`test_import_uses_real_remote_host_and_appends_once` with a failed first attempt), `test_gate_box.py`, `test_show.py`.
- `docs/as-built/merge-gate-and-test-speed.md` — "Failed module output": the `buildlog query` for the path, then `zcat`.

**Gotchas:**
- The cloud gate builds its record on the box, so the box cannot write a natedev log; the import on natedev keeps the logs.
- A new required field on `Execution` or `write_gate_run` reaches every test fixture that builds a gate record; type-check every file that names `gate_record`.

**Ruled out:**
- Prune code for the logs: the build log prunes neither records nor logs (builds showrunner, 2026-11-06).

### Phase 35 — One full rebuild of the build log index has a measured memory cost  · status: done

#### As-built

Measurement only; no code changed. Two full rebuilds ran on private copies of the live JSONL (443 files, 1.16 GiB, about 276,000 records; a 3.0 GB index with 4096-byte pages).
- SQLite 3.51.2 reads `PRAGMA cache_spill = N` as a boolean through 8 bits. `INDEX_SPILL_PAGES=65536` gives 0, so `open_for_update` turns spill off. 65535 stays on; `PRAGMA cache_spill = ON` after the size keeps 65536 in effect.
- Spill off (as shipped): peak RSS 3.75 GiB, 132.7 s wall, 102 CPU s. Six readers waited 9.4–10.9 s at commit and read the new index.
- Spill on (trial wrapper only): peak RSS 1.34 GiB, 252.7 s wall, 110 CPU s, under a higher load. The first spill came at 11.5 s. Readers started near the first spill, the middle and the end waited until commit (43.6–245.8 s) and read the new index.

**Files:**
- `config/buildlog.conf` — the `INDEX_SPILL_PAGES` basis gives both runs; the value stays 65536.
- `docs/as-built/buildlog-memory-stalls.md` — "Calibration and gotchas": the full-rebuild entry.

**Gotchas:**
- CLI readers (`buildlog query`, `test_times show`) wait on the update file lock for the whole rebuild, with spill on or off; neither run measured the 30 s direct SQL timeout.
- A value change to `INDEX_SPILL_PAGES` needs the user's approval first (builds showrunner, 2026-11-08).

### Phase 36 — A full rebuild of the build log index uses about a third of the memory · status: done

#### As-built

`INDEX_SPILL_PAGES` takes effect: an index update spills at 65,536 changed pages.
- `scripts/buildlog/index.py` `open_for_update` runs `PRAGMA cache_spill = {INDEX_SPILL_PAGES}` and then `PRAGMA cache_spill = ON`. SQLite reads N as a boolean through 8 bits, so 65536 alone turns spill off; `ON` sets the flag and keeps the threshold.
- `test_update_enables_the_configured_spill_threshold` checks that the writer reports the configured threshold.
- One full rebuild (443 files, 276,466 records, load 47.2 to 68.9): peak RSS 1,378,896 KiB, 154.33 s wall, 98.01 CPU s, first spill at 9.975 s.

**Files:**
- `scripts/buildlog/index.py` — `open_for_update` enables spill.
- `scripts/buildlog/test_index.py` — the spill threshold test.
- `config/buildlog.conf` — `INDEX_SPILL_PAGES` basis comment; the value stays 65536.
- `docs/as-built/buildlog-memory-stalls.md` — "Full rebuilds and spill, 2026-11-08": the measurements and the reader cost.

**Gotchas:**
- In rollback-journal mode a spill takes an exclusive lock. Direct readers that skip `index.update()` wait 30 s and fail (`database is locked`): `buildlog costs`, `buildlog moments` and charts. `complete_gate_index_in` raises `NoCompleteGateIndex` after 1 s; the step estimator falls back after 5 s.
- A value change to `INDEX_SPILL_PAGES` needs the user's approval first (builds showrunner, 2026-11-08).

### Phase 37 — Readers get their answer during a full rebuild of the build log index · status: done

#### As-built

The build log index uses WAL mode, so a spilled update takes no exclusive lock and readers get the last committed snapshot.
- `scripts/buildlog/index.py` `open_for_update` runs `PRAGMA journal_mode = WAL` on a new and an existing index, and raises `sqlite3.OperationalError` when SQLite does not return `wal`. A rebuild removes the old index, enables WAL, then creates the schema.
- One full rebuild (444 files, 276,884 records, load 108.2 to 61.3): peak RSS 1,386,436 KiB, 240.09 s wall, 119.54 CPU s, largest WAL 3,094,976,992 bytes. Four readers started after the first spill exited 0 in 0.097-1.709 s.
- `read_only_evidence` (`scripts/aws_target/measurement_test_support.py`, aws-unit) leaves out only the index `-wal` and `-shm` sidecars in the build log root.
- The two lock tests in `scripts/test_times/test_show.py` hold `PRAGMA locking_mode = EXCLUSIVE` and assert that a reader gets `database is locked`.

**Files:**
- `scripts/buildlog/index.py` — `open_for_update` sets WAL.
- `scripts/buildlog/test_index.py` — WAL for a new and an existing index; committed rows during spill and after the writer closes.
- `config/buildlog.conf` — `INDEX_SPILL_PAGES` comment with the WAL measurement; the value stays 65536.
- `docs/as-built/buildlog-memory-stalls.md` — the WAL measurement, the sidecar and conversion facts.
- `scripts/aws_target/measurement_test_support.py`, `scripts/buildlog/test_aws_report.py` — the sidecar rule and its test.
- `scripts/test_times/test_show.py` — the two lock tests.

**Gotchas:**
- A `mode=ro` reader creates the `-wal` and `-shm` sidecars and cannot remove them; an update that closes with no other connection open removes them.
- Conversion from `delete` to WAL waits for open read transactions, up to the 30 s busy timeout.
- `BEGIN EXCLUSIVE` does not block WAL readers; a test that needs a blocked reader uses `PRAGMA locking_mode = EXCLUSIVE`.
- A full rebuild needs about 3 GB more disk for the WAL file. During the first rebuild after a schema change, readers see an empty index.

**Ruled out:**
- A change to `INDEX_SPILL_PAGES`: a value change needs the user's approval (builds showrunner, 2026-11-08).

### Phase 38 — Each moved admission name has one import path · status: done

#### As-built

`scripts/lint/memory_admit.py` re-exports none of the 227 admission names that moved to `scripts/build_queue`. Each caller imports a name from its defining module (`admission_records`, `step_estimator` and the other `build_queue` modules). This includes the Python source that tests write at run time.
- Code behavior and test assertions are unchanged. `memory_admit.py release` loads the same 24 modules.
- `scripts/buildlog/mac_idle.py` (verify-unit) changes only its type import and annotations.

**Files:**
- `scripts/lint/memory_admit.py` — no re-exports; it imports only the names it uses.
- `scripts/build_queue/*`, `scripts/aws_target/{aws_target,cloud_gate,control_test_support}.py`, `scripts/production/merge_gate.py` and its tests, `scripts/hooks/test_brp_launch_gate.py`, `scripts/mac_test/loopback_proof.py`, `scripts/buildlog/{mac_idle,width_trial,test_memory_window}.py`, `scripts/direct/test_verify_{build_key,release_ack,token_reservation,wait_holder}.py`, `scripts/direct/verify_token_wait_test_support.py`, `scripts/lint/{memory_test_support,test_memory_admit}.py` — callers on the one path.

**Gotchas:**
- The static import walk does not see Python source in string literals. Search the scripts that tests write at run time for the old path.
- A new caller that reads a moved name through `memory_admit` fails basedpyright with an unknown attribute.

### Phase 39 — Cloud gates show as normal rows on /build:queue and the build page · status: done

#### As-built

- Cloud gates are rows in the one queue table (`append_cloud_row` in `queue_view.py`), with no separate Cloud group. The Head line gives the cloud slot count, for example `cloud 1 of 1 slots`.
- A running cloud row shows machine `aws`, its vCPU count and its progress text in the memory column. A waiting row, local or cloud, leaves the machine and vCPU cells blank. A running local row shows `os.cpu_count()` in the vCPU column.
- A late running cloud row shows a red ` · no update for <age>`. `CloudUpdateLate.seconds` holds the age, and `cloud_update_age` formats it for the text view, the step card and the feed field `late_age`.
- The build page shows the same rows in the feed order, with `#` and `vCPU` columns. `CloudWorkRow` in `memory_feed.py` has `machine: Literal["aws", ""]`, `late_age` and `stale`. CI rows stay last on the page, as before.
- The step card keeps its Cloud lines.

**Files:**
- `scripts/build_queue/queue_view.py` — cloud rows, the vCPU column, `CloudUpdateLate`, `cloud_update_age`.
- `scripts/build_queue/memory_feed.py` — the `CloudWorkRow` fields for the page.
- `scripts/build_queue/memory_live.html` — cloud rows and the vCPU column on the page.
- `scripts/build_queue/test_queue_view.py`, `test_queue_card.py`, `test_memory_feed.py`, `test_build_queue.py`, `memory_page_proof.mjs`, `golden/*.txt` — tests and goldens.
- `docs/as-built/build-memory-admission.md` — the queue table section.

**Gotchas:**
- A test that renders a vCPU column patches `os.cpu_count` to 17 (`QueueViewFixtureTests`, `QueueTests.setUp`). The real count differs per machine.
- Format an age once, from seconds. Text that is formatted and then parsed back fails at `updated_at=0`.

### Phase 40 — /build:queue retires when the build page covers it · status: done

#### As-built

- `build_queue.py` with no control word and no `--json` prints `build queue: the queue view is on the build page; run /build:page` and exits 3. It reads no admission state first.
- The `--json` route that the unit-board queue pane runs (`mods/unit-board/hooks/register.tsx:326`) and its selector cards do not change. `bump`, `clear` and `admit-head` do not change.
- The feed has `mac_idle_alert` (from `mac_idle_alert.alert_line`, empty when there is no alert) and `priority_text` (`head_facts.priority_text`). The build page shows the alert as a red line above the title and the priority line above Head.
- Each `queue_view` formatter still has a caller (the `--json` route, the card or the feed), so `queue_view.py` did not change.

**Files:**
- `scripts/build_queue/build_queue.py` — controls, the `--json` route and the plain-view refusal.
- `scripts/build_queue/memory_feed.py`, `scripts/build_queue/memory_live.html` — the alert and priority on the page.
- `commands/build/queue.md`, `commands/build/status.md`, `docs/as-built/build-memory-admission.md`, `docs/as-built/build-targets.md` — the controls, the pane and the page.
- `scripts/build_queue/test_build_queue.py`, `test_queue_evidence.py`, `test_memory_feed.py`, `test_queue_view.py`, `memory_page_proof.mjs` — tests of the plain route moved to `--json` with the same expected content.

**Gotchas:**
- The `--json` route renders with colour. A test compares its text exactly with a colour render. `test_queue_card` compares the non-colour page exactly with `golden/card_1.txt`.

### Phase 41 — The vault sync keeps the Mac main ref and files its stop question · status: done

#### As-built

- The hourly vault sync fetches `refs/heads/main:refs/remotes/mac/main`. With the global `fetch.prune true`, the full source name keeps the ref on each run.
- A vault stop question passes `--production` with the name from `config/build_targets.conf` key `vault.question_production` (`natedev`), read at use.
- Replies, printed text, stored state and the question text are unchanged.

**Files:**
- `scripts/buildlog/vault_pull.py` — the fetch refspec; the production name from config.
- `scripts/buildlog/test_vault_pull.py` — two syncs in a row with `fetch.prune true`, with and without a new Mac commit; the registration reads the config value at use.
- `config/build_targets.conf` — `vault.question_production=natedev`.

**Gotchas:**
- A URL fetch into a fixed remote-tracking ref with a short source name (`main:refs/remotes/mac/main`) lets prune delete that ref on each second fetch. When the source moved, that fetch fails with `cannot lock ref`.
- `update_registration.py --production` takes the production name. A production doc path fails with "no showrunner staff row" when the staff row names another copy of the doc.

**Ruled out:**
- A catch or default for a deleted `vault.question_production` key: the key ships in the same commit as the code that reads it.

### Phase 42 — Build log tests compare file times with a file-system clock · status: done

#### As-built

- `test_record_rerun.py` `test_late_finish_refreshes_completion_before_rename_and_survives_a_stop` takes `now` from the `st_mtime` of a file it touches first, then removes the file.
- `test_record.py` takes `released_at` from the `meminfo` `st_mtime` after the write that releases the memory wait.
- No sleep and no tolerance. The assertions do not change.

**Files:**
- `scripts/buildlog/test_record_rerun.py`
- `scripts/buildlog/test_record.py`

**Gotchas:**
- The kernel stamps file times from a coarse clock that can be one tick behind `time.time()`. A test that compares a file time with `time.time()` fails now and then; a cloud gate saw a 0.16 ms miss.

### Phase 43 — The build page proof removes the folders it makes · status: done

#### As-built

- `memory_page_proof.mjs` removes its `memory-page-proof-*` output folder and its `memory-proof-chrome-*` profile when the proof ends, on pass and on failure.
- A folder given as `argv[2]` belongs to the caller and stays. The success line names `output` only for that folder.
- The script has no folder scan and no signal handler.

**Files:**
- `scripts/build_queue/memory_page_proof.mjs` — the build page proof.

**Gotchas:**
- Chrome refuses a long temporary socket path. Run the proof under a short `$TMPDIR` when the default path is long.

### Phase 44 — A vault stop files a question only when the next hourly try also stops · status: done

#### As-built

- The Mac script's `retry_git` prints Git output only for the final failure: exhausted lock retries, or an error that is not a lock. A vault stop shows the Git error once.
- `_vault_locked` files a question only when an outage was already open before the run, for every stop cause. A first stop opens the outage with `asked` false.
- The 24-hour Mac unreachable grace still applies. A failed `ask` retries on the next run. A good sync after a first stop files no question and withdraws none.
- Any run of `buildlog vault` counts as the next try, also a manual one.
- Exit codes did not change. The rule has no config key: one try is not a tunable.

**Files:**
- `scripts/buildlog/vault_pull.py` — the sync and its question rule.
- `scripts/buildlog/test_vault_pull.py` — the rule for each stop cause, and the single error line.
