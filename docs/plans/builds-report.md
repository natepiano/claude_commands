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

### Phase 0 — The builds merge keeps report-unit's head times on queue-unit's new admission reader  · status: todo

#### Work Order

**Goal:** the phase-start merge of origin/builds 50c45192 lands with report-unit's unlanded phase 8 (b261673a: head times, cargo-token waits, sccache rows) ported into queue-unit's refactored admission reader, so the showrunner can merge builds-report without a conflict.

**Spec:**
- Keep queue-unit's structure (`exclusive_queue`, `gather_queue_inputs` → `QueueInputs` → `queue_read_from`, the lock-free snapshot) as the base. Port `head_since` and its write under the admission lock onto it, with one live-row ordering. Keep the cargo-token left and back times in `memory_gate.sh`.
- Keep every test from both sides. Merge both sides' as-built text so it matches the merged code.

**Files:**
- `scripts/lint/memory_admit.py`
- `scripts/lint/memory_gate.sh`
- `scripts/lint/test_memory_admit.py`
- `scripts/build_queue/test_build_queue.py`
- `scripts/build_queue/test_build_why.py`
- `docs/as-built/build-memory-admission.md`
- `docs/as-built/unit-board.md`

**Workers:** impl3 writes `memory_admit.py`, `memory_gate.sh` and the two as-built docs. test2 writes `test_memory_admit.py`, `test_build_queue.py` and `test_build_why.py`.

**Acceptance gate:**
- No conflict markers remain.
- `python3 -m unittest scripts.lint.test_memory_admit scripts.build_queue.test_build_queue scripts.build_queue.test_head_facts scripts.build_queue.test_build_why` passes.
- `basedpyright` on each slot's Python files reads 0 errors, 0 warnings, 0 notes.

### Phase 1 — The build report compares build rates before and after each build moment  · status: done

#### Work Order

**Goal:** one command answers whether the new build rules cut build load. It prints per-day rates before and after Moment 1 (and Moment 2 when it lands), split at every natedev build event, the floor sweep and every power profile change, with the clone rebuilds on their own rows. It uses the comparison the Power profile section uses today; that section becomes the general one.

**Spec:**
- **One list of moments.** A new module `scripts/buildlog/moments.py` holds `MOMENTS`, a typed tuple. Each moment has:
  - its UTC instant;
  - a short name;
  - a scope (`all` or `hana`);
  - an optional typed sdlc baseline, for a moment that carries quoted "before" or "after" figures.

  Entries:

  | Moment | Time | Scope |
  | --- | --- | --- |
  | herdr cgroup move | 2026-10-28 09:01 EDT | all |
  | baloo content indexing off | 2026-10-28 09:24 EDT | all |
  | hana opt-level 1 | 2026-10-29 02:59 EDT | hana |
  | hana Bevy dylib | 2026-10-29 05:33 EDT | hana |
  | Floor sweep (e3b8e391) | 2026-10-29 14:26 EDT | all |
  | Build rules 1 (Moment 1) | 2026-10-31 06:50 EDT | all |

  Moment 1 carries sdlc's figures for the week before it: run hours 708, hana 595; wait hours 2125, hana 1329; hana nextest 246/day, 16% reruns, 46% queue-hidden repeats, 62% failed; hana clippy 160/day; load ratios 12.9 nextest and 8.4 clippy per phase (134 phases); same-worker reruns after a pass 221 runs and 33.3 h, fail then fail 50–55 h; clone rebuilds 805 runs, 231 of 327 nextest hours. The floor sweep carries sdlc's clone repeat figures: 52.7% before, 29.8% after, 124 clone rebuilds after. Moment 2 is added as one entry when its hash lands. Power profile changes stay observed from `samples` (`profile_changes`) and merge into the same ordered list at read time. Nothing else lists moments.
- **One hana set.** `moments.py` holds the one definition of the hana set, sdlc's SQL: a step is hana when its repo, repo_path, repo_identity, cwd, worktree, branch, target_folder, argv or build_base contains `hana`, `tool-based-ui`, `catalyst` or `fairy_dust` (case-insensitive); or when its repo is not cargo-liner, cargo-handler, cargo-tile, cargo-berth, bevy_brp or .claude, and its session or unit_session is majority-hana, or its call is a hana call. The exact SQL is in the run's `phases/1/sdlc_recheck.sql` (view `nat`); copy its predicate, with the window as a parameter. Sessions and calls are classified over the window being measured. Every rate row and every window uses this one set. The report names it once.
- **One comparison.** Generalize `power_profile_section` into one section, `### Before and after`, over the merged list, newest first. Keep today's per-kind p50 table for each moment as it is: kind, caller, rebuild class, runs and p50 on each side, and Change with `MIN_PROFILE_RUNS`. Keep its window rule: before = 7 days, cut at the previous moment; after = up to the next moment or the report end. No second copy of the window logic.
- **Rates per window.** For each moment, add a rates table beside the p50 table, with Before/day, After/day and Change. Rows (hana rows use the hana set; a hana-scoped moment shows hana rows only):
  1. run hours, all and hana; wait hours (`mem_wait_s`), all and hana;
  2. hana nextest steps per day and % failed (`status <> 0`);
  3. reruns: % of hana nextest steps whose start is 0–10 min after the previous nextest step's end in the same folder (cwd without `/target/verify-doc`);
  4. queue-hidden repeats: % of hana nextest steps whose submit (start − `mem_wait_s`) is at most 600 s after the previous nextest step's end in the same folder, with a different call_id;
  5. same-worker reruns: hana nextest steps whose submit is at most 60 min after the previous nextest step's end in the same session. Two rows: after a pass (both passed), runs/day and hours/day; fail then fail (both failed), runs/day and hours/day;
  6. hana clippy steps per day;
  7. load ratios: hana nextest steps and hana clippy steps in the window, each over the natedev `phase_finished` events in the window whose working_dir matches the hana set's name patterns;
  8. clone rebuilds, on their own rows: hana nextest steps with `crates_compiled >= 300` per day, their nextest hours per day, and clone repeats: % of clone rebuilds whose previous nextest step in the same session was also a clone rebuild that ended at most 4 h before.
- **Definitions.** Print each definition once under the section: natedev only; the hana set (named, in one line); run hours count every step kind, sweep included; each rate row's definition above; phases come from natedev's progress history only, and the Mac's phases are not counted; per-day = total ÷ window days. A window shorter than 1 day prints `thin` beside its rates.
- **sdlc's figures.** For Moment 1, also print the 7-day baseline before it. Wherever an sdlc figure exists for a row, print it beside ours as `ours (sdlc N)` when they differ by more than 5% (relative). Under the table, print one note: fail then fail differs (ours about 80 h, sdlc 50–55 h); load ratios differ because sdlc's 134 phases include the Mac.
- **On demand.** Add `buildlog moments [YYYY-MM-DD]` to print only this section; it is fast and reads the index read-only. `buildlog report` prints the same section in place of Power profile. No timer and no notifier.

**Files:**
- `scripts/buildlog/moments.py`
- `scripts/buildlog/report.py`
- `scripts/buildlog/cli.py`
- `scripts/buildlog/test_moments.py`
- `scripts/buildlog/test_report.py`

**Workers:** impl3 writes `moments.py`, `report.py` and `cli.py`. test2 writes `test_moments.py` and the `test_report.py` changes.

**Acceptance gate:**
- `basedpyright` on each slot's files reads 0 errors, 0 warnings, 0 notes.
- `python3 -m unittest scripts.buildlog.test_moments scripts.buildlog.test_report` passes, with fixture indexes and a fixture progress history that cover:
  - the merge of a profile change into the moment order;
  - the hana set: a direct match, a majority-hana session, a hana call, and an excluded repo;
  - each rate row, each definition, `thin`, and `ours (sdlc N)`;
  - the cut at the previous moment;
  - the clone rows and clone repeats;
  - a hana-scoped moment, which shows hana rows only.
- Live check by the director: `buildlog moments` on natedev prints, for sdlc's window (2026-10-24T10:40:20Z to 2026-10-31T10:33:06Z) read through the same functions, within 1% of the recheck: hana wait 1329.0 h; hana clippy 160.4/day; 1724 hana nextest and 1123 hana clippy steps; reruns 16.2%; queue-hidden repeats 45.8%; same-worker after a pass 218 runs and 33.7 h. It finishes in under 5 s.

**Constraints:**
- Workers make no worktree commits.
- Tests never read the live index or progress history, and never write either.
- Tests run with `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY -u STAFF_OPERATION_FD`.
- Build rules from 2026-10-31: fix every failure you can see before you rerun, and do not run a test after each small edit. After a pass, do not run again until your edits are done. Run basedpyright once, at the end.
