# builds-report

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The build report shows build rates per day before and after each build moment: the new build rules (Moment 1, Moment 2), the natedev build events and the power profile changes, from one list of moments.

> **As-built disposition: amend** — `docs/as-built/buildlog-memory-stalls.md` (report sections); `docs/as-built/build-followups.md` (rebuild bins and the clone-rebuild count)

> **Production: builds** — unit `report-unit`; production doc `docs/plans/builds-production.md`
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
- **Test:** `PYTHONPATH=scripts/buildlog env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY -u STAFF_OPERATION_FD python3 -m unittest <module>` from the worktree root.
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

### Phase 15 — The build page ranks the top costs with the action that cuts each  · status: todo
**Depends on:** Phase 14; queue-unit's Phase 10m (it moves the Build Memory page and its feed into the repo)

The user, 2026-11-04, change review C948 (`buildlog costs`): "send this to builds to review - i'm not sure about this". The builds showrunner ran it. The data is useful: memory wait is 236 step-hours a day, eight times the next cost. The output does not tell the user what to do. Ruling:

1. Delete the `buildlog costs` command. The ranking moves onto the one build page the user uses. builds names that page.
2. Show at most the top 3 costs. Each line gives the cost in plain words and hours, then the action that cuts it and its owner. The action text lives in config, not code.
3. Show hours in the user's zone. No seconds, no UTC, no microseconds.
4. Move the Definitions block into the as-built doc.
5. Check the memory-wait number first. 236 h a day means about 10 steps wait at all times. queue-unit's 10l bug (on main now) made a waiting step with the AWS hook hold its grant and never start. Say how many of the 236 h came from that bug.
6. The build history charts (C950) go on the same page. No second page. Phase 22 made `buildlog charts` publish its own page and print its tailnet link. After this phase, `buildlog charts` publishes no page of its own: it prints the tailnet link of the shared build page. With no link available, it prints the reason and exits non-zero.

The page is queue-unit's Build Memory page (`memory_live.html`, fed by `memory_feed.py`); queue's 10m moves it into the repo. Agreed with queue-unit 2026-11-04: report-unit gives typed data, no HTML, from `charts.page_section(days)` and `costs.top_costs(n=3)`. queue's feed calls them and its page draws them. A 7-day read stays under 1 s. Send queue-unit the signatures and types when they land.

Memory-wait check, done 2026-11-04 (`memory_wait_check.md` in the phase folder): the 236 h/day is a 7-day average that old days drive. Oct 29-31 had 347-533 h/day; Nov 1-3 had 7-90 h/day. The 10l bug adds at most 125 h in total (Nov 2-4). `costs.py` counts each wait correctly. builds ruling: the top 3 rank the last 2 days, so a fixed cost does not stay on top. Each line shows one number. It shows the 7-day value beside it only when the two differ by more than half. The window length lives in config.

Each fix returns as a new Change-Review row.

Acceptance: one page request through queue's feed calls both providers for 7 days in under 1 s total. Interface tests pass `NoDataYet`, `CostUnavailable` and `MacIdleUpperBound` through to the feed unchanged. Phase 16's `test_machine_aliases_read_sync_configuration_once` stays green. A test proves `buildlog charts` prints the shared page's tailnet link and writes no page; a test proves a missing link prints the reason and exits non-zero.

**Constraints from prior phases:** `costs.machine_name` reads `sync_config()` once and calls `record._target_hosts` (Phase 16).

**Workers:** 1 writer (report-unit's providers and their interface tests). queue-unit owns the page and the feed integration test.

**Files:** `scripts/buildlog/costs.py`, `cli.py`, `charts.py`, their tests, `config/buildlog.conf` (action text and ranking window) and the test that reads it, `docs/as-built/` (Definitions).

### Phase 16 — Each build step spends less CPU to write its build-log row  · status: done
**Depends on:** none (from next items; showrunner disposition 2026-10-30: next)

#### As-built

- `record.py` loads config, target, hash, gzip, json, store and url parsing only on the paths that use them. The pending writer uses `os.open(O_EXCL, 0o600)` and `os.replace`; ids come from `os.urandom`.
- One owner for each rule: `record.remote_identity` (repository key; admission's `git_identity` calls it), `record.recorded_repository_key` (origin remote, else `git-root:<first root>`), `parse.test_threads` (admission keeps the name `memory_admit.test_threads`), `record._target_hosts` (one `sync_config()` read for all targets).
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

### Phase 24 — Mac offload reads the repository key without loading memory_admit  · status: todo
**Depends on:** queue 10z on builds (done, b7ad7675c).

#### Work Order

**Goal:** `offload.repository_identity` gets its key from the recorder's light reader, so a call runs no `memory_admit` code. The keys stay the same. Director amendment 2026-11-05: offload still loads `memory_admit` through top-level imports in `sweep` -> `build_hold` -> `build_queue.admission_service`/`admission_protocol`. Those files belong to other units; that chain went to the builds showrunner.

**Spec:**
- `scripts/mac_test/offload.py:898-914` `repository_identity(repo_root)` now imports all of `memory_admit` (lazy, line 907-910) and calls `memory_admit.git_identity(["git"], str(source.parent))`. That runs `record.git_directory`, `record.git_facts` (2-3 git processes) and `git remote get-url origin`, then `git rev-list --max-parents=0 --reverse HEAD` when no remote exists. Only `identity.key` is used.
- Replace that call with `record.recorded_repository_key(["git"], str(source.parent))` (`scripts/buildlog/record.py:530`). It runs `git remote get-url origin`, normalizes with `record.remote_identity` (the same function `git_identity` uses), else returns `"git-root:" + <first root commit>`, else `store.UNKNOWN_IDENTITY`.
- `store.UNKNOWN_IDENTITY` maps to `KnownRepository(name)` (today's result for `UnknownRepository`). Any other value maps to `KnownRepository(name, key)`.
- Keep the `sweep.repo_name`, `sweep.git_common_dir`, `sweep.source_git_dir` and `config` checks unchanged.
- Remove the `memory_admit` import from offload.py when no other use remains. Check with LSP `findReferences`.
- Import `record` the way offload.py imports its other `scripts/buildlog` modules (`sys.path` already holds `scripts/buildlog`, line 29). Check that importing `record` from offload.py does not form an import cycle.

**Files:**
- `scripts/mac_test/offload.py` — `repository_identity` uses `record.recorded_repository_key`.
- `scripts/mac_test/test_target_slots.py` — a test that pins the keys: a repository with an `origin` remote (scp form and https form give one normalized key), and one with no remote (`git-root:<root commit>`). Each expected key is a literal from the fixture, not a call to the code under test. The existing identity tests (lines 160-230, 349) stay unchanged.

**Workers:** 1 writer — nothing splits; `recorder` owns both files.

**State:** none.

**Constraints from prior phases:** The recorder no longer loads `memory_admit` (Phases 16 and 18). `record.recorded_repository_key` and `record.remote_identity` exist (Phase 16). Repository keys must stay unchanged; queue and the Mac copy records key on them.

**Acceptance gate:**
- `scripts.mac_test.test_target_slots`, `scripts.mac_test.test_offload` (if present), `scripts.mac_test.test_linux_runner`, `scripts.mac_test.test_copy_watch` and `scripts.mac_test.test_audit` green, from the worktree root with and without `PYTHONPATH`.
- offload.py has no `memory_admit` import, and `repository_identity` calls no `memory_admit` function. The summary names the import chain that still loads `memory_admit` (from `python3 -X importtime`).
- CPU per call before and after: a fresh process per run, 10 runs each, interleaved, on this worktree and on one checkout with no remote. Report median own+child CPU and wall.
- `basedpyright` on both files: 0 errors, 0 warnings, 0 notes.

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
