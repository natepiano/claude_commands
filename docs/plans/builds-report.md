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

**Files:**
- `scripts/lint/memory_admit.py` — the admission reader, with `queue_rows()` and `write_head_since()`
- `scripts/lint/memory_gate.sh` — cargo-token left and back times
- `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_build_queue.py`, `scripts/build_queue/test_build_why.py` — both sides' tests
- `docs/as-built/build-memory-admission.md`, `docs/as-built/unit-board.md` — the merged as-built text

### Phase 1 — The build report compares build rates before and after each build moment  · status: done
**Depends on:** Phase 0

#### As-built

- `scripts/buildlog/moments.py` is the one place for build moments and the hana set:
  - `MOMENTS` holds 6 fixed moments (herdr cgroup move, baloo off, hana opt-level 1, hana Bevy dylib, the floor sweep, Build rules 1). Moment 1 carries sdlc's week baseline; the floor sweep carries sdlc's clone repeat figures. Moment 2 is one new `MOMENTS` entry when its hash lands.
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
