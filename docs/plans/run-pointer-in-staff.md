# Run pointer in staff

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A director's open `/unit:direct` run folder moves from a mark file in `/tmp/claude/unit/active` into the director's staff row. The mark folders go.

## Source

2026-11-10, the user via the sdlc showrunner: "go. Move the open-run pointer into the staff row and remove /tmp/claude/unit/active."

The showrunner's scope:
1. Store a director's open /unit:direct run folder in its staff row. prepare_session.sh sets it; end_session.sh clears it.
2. Change every reader of the marks to read the staff row: unit_runs.py, build_hold.py, worker.py, session_staff.py, progress_history.py, unit_status.sh, unit_labels.py, agents/shutdown.py.
3. Migrate once at deploy: copy each live mark into its row, then delete /tmp/claude/unit/active and the former delegate/active folder.
4. Tests use their own staff file, never live units.

## What exists today (2026-11-10)

- `prepare_session.sh` runs `unit_runs.py activate <run> <session id>`. It writes `/tmp/claude/unit/active/<session id>` (first line: the run folder) and `<run>/active_marker` (the mark path). `progress_history.py start-run` calls `unit_runs.activate` again.
- `end_session.sh` runs `unit_runs.py end <run>` for each held run. It removes the mark only while the mark still names that run.
- Readers find a session's run with `unit_runs.marker`, `unit_runs.held` and `unit_runs.active_dirs`, or read the folder directly. Overrides `UNIT_ACTIVE_DIR` and `PLAN_DELEGATE_ACTIVE_DIR` point tests at their own mark folder.
- On 2026-11-10 the deploy one-off for the old marks ran. 27 marks remained. Most name runs whose sessions ended long ago, because a mark is a file that nothing owns.
- The staff record (`~/.local/state/staff/staff.json`, 148 KB, owner `scripts/production/staff.py`) already holds each director's row, with `current_session`, `worktree`, `doc` and holds. `STAFF_FILE` points a test at its own record.

## Decisions

- **The row is the one place** (user, 2026-11-10). No mark file, no `active_marker` file and no mark folder remain. New mode replaces old at once; no reader keeps a fallback to the mark folders (memory `new-mode-replaces-old-at-once`).
- **Field shape** (plan author): `StaffRow.run: OpenRun | NoRun`. `OpenRun` holds the run folder path. JSON key `run`, a string path; an absent key is `NoRun`. Run folders are on natedev only, so the field holds no machine.
- **Clear only your own run** (plan author): `clear_run(identity, run)` clears the field only while it still names that run. The same rule as the mark today, so a late end of an old run cannot clear a new run.
- **Old data: one-off at deploy** (memory `migrate-once-at-deploy`). Main code reads no mark. The one-off lives outside the repo, in a `scratch.py` task with an `until:` time, because the tmp sweep removes a folder idle for 4 hours (2026-11-09: it removed the last deploy one-off before it ran).
- **Hooks stay fast** (three gods). The hooks read the run on each tool call. They read the one row they need with the cheapest staff read that exists. They do not parse the whole record on each call unless a measure shows the cost is under 5 ms.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-10T13:28:31.690+00:00
- **Stack:** Python 3.13, standard library only; Bash; Markdown.
- **Key files:**
  - `scripts/production/staff.py` — the staff record: `StaffRow`, row edits through `_transaction`, the CLI.
  - `scripts/lib/unit_runs.py` — run folders, marks today, `held`, `director`.
  - `scripts/direct/prepare_session.sh`, `scripts/direct/end_session.sh`, `scripts/direct/progress_history.py` (`start-run`) — the writers.
- **Test lanes:** `test_*.py` beside each script.
- **Test:** targeted test: `~/.claude/scripts/lib/py scripts/production/targeted_test.py` from the worktree root (memory `shared-module-full-gate`).
- **Lint:** `basedpyright scripts/lib scripts/direct scripts/production scripts/message scripts/shutdown scripts/agents scripts/hooks scripts/herdr scripts/build_hold scripts/usage scripts/lint` reports `0 errors, 0 warnings, 0 notes`.
- **Invariants:**
  - Tests use their own staff file through `STAFF_FILE` and their own run root. They never read or write live staff, live run folders or live units (user, 2026-11-05).
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`).
  - Add no check or scan that the Work Order does not name (user, 2026-11-03).
  - All text uses Simplified Technical English (user, 2026-10-18).

## Phases

### Phase 1 — The staff row holds the open run  · status: done

**Depends on:** none.

#### Work Order

**Goal:** a director's open run folder is in its staff row. Every reader gets it from there. The mark folders and the `active_marker` files are not written or read.

**Spec:** change no file outside Files without a line in the summary; another test file beside a listed script is in scope.
1. `staff.py`:
   - Add `OpenRun(path: Path)` and `NoRun`, and `StaffRow.run: OpenRun | NoRun`, default `NoRun`. JSON key `run`: a string path; absent means `NoRun`.
   - Add `set_run(identity: StaffId, run: OpenRun) -> StaffRow` and `clear_run(identity: StaffId, run: Path) -> StaffRow`. `clear_run` clears only while the row still names that run (compare resolved paths). Both go through the existing row transaction.
   - CLI: `staff.py set-run <session id> <run folder>` and `staff.py clear-run <session id> <run folder>`. The session id finds the row by `current_session`. No row: print one line and exit 1.
2. Writers:
   - `prepare_session.sh`: `staff.py set-run` replaces `unit_runs.py activate`.
   - `progress_history.py start-run`: `staff.set_run` replaces `unit_runs.activate`.
   - `end_session.sh`: `staff.py clear-run` for each held run replaces `unit_runs.py end`. Its messages say "run pointer" in place of "marker". A failed clear prints one line and sets the exit status, as today.
3. `unit_runs.py`:
   - Add `open_run(session_id: str, *, record=RunStaffAtUse.LOCAL) -> Path | None`: the run in the row whose `current_session` is that session.
   - `held` reads `open_run` in place of the mark and drops `active_override`.
   - Remove `activate`, `end`, `marker`, `active_dirs`, `marker_lock`, `marker_must_stay`, `ACTIVE_MARKER_FILE`, `former_owners`, `removal_check`, the `activate`, `end` and `removal-check` subcommands, and the `active` folder skip in `run_folders`. Update the module docstring.
4. Readers. Change each to `unit_runs.open_run`, `unit_runs.held` or the row:
   - The showrunner's list: `scripts/build_hold/build_hold.py`, `scripts/production/worker.py`, `scripts/production/session_staff.py`, `scripts/direct/progress_history.py`, `scripts/production/unit_status.sh`, `scripts/message/unit_labels.py`, `scripts/agents/shutdown.py`.
   - Also found by the plan author: `scripts/direct/assign.py`, `scripts/production/promote_showrunner.py`, `scripts/production/unit_lookup.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/inventory.py`, `scripts/shutdown/shutdown.py`, `scripts/herdr/worker_panes.py`, `scripts/message/roster.py`, `scripts/usage/agents_log.py`, `scripts/hooks/pre-tool-use-berth.py`, `scripts/hooks/direct_run.py`, `scripts/hooks/conversation_pause.py`, `scripts/build_hold/override_guard.py`, `scripts/build_hold/override_guard.sh`, `scripts/lint/tmp_sweep.py`.
   - Run `grep -rn -E 'active_dirs|unit_runs\.(marker|activate|end)|ACTIVE_DIR|/active\b|"active"' scripts commands docs/direct config` and change each remaining reader. Report each line you leave and why.
   - Remove the `UNIT_ACTIVE_DIR` and `PLAN_DELEGATE_ACTIVE_DIR` overrides, which only point at a mark folder. A test uses `STAFF_FILE` in their place. Keep the run-root overrides.
5. Hooks: each hook reads the run with one row read. Measure the hook's added time on the live staff record (read only) and put the number in your summary.
6. Docs: change each statement about the mark folders to the row: `commands/plan/shrink.md`, `commands/unit/direct.md`, `docs/direct/*.md`, `docs/as-built/hierarchy.md`, `docs/as-built/session-notifier.md`, `docs/plans/sdlc-devtools-path-copies.md`, and any other file the grep in item 4 finds.
7. Deploy one-off (outside the repo). Make its folder with `~/.claude/scripts/lib/scratch.py new --until <date 7 days ahead>`; do not use `--worker`. It takes `--checkout <path>` and imports the code from that checkout. For each mark file in `/tmp/claude/unit/active` and `/tmp/claude/delegate/active`:
   - Find the row whose `current_session` is the mark's name.
   - When the row exists, the run folder exists, the run's state is not finished (`completed`, `stopped`, `error`, `ended`), and the row has no run: `set_run`.
   - Print one line for each mark: copied, or skipped with the reason.
   Then remove both `active` folders and each `<run>/active_marker` file, and print the counts. Report its path and command in your summary. Do not run it.
8. Tests: change the existing mark tests to rows in `STAFF_FILE` fixtures. Add rows to the existing staff tests for `set_run`, `clear_run` of the same run, and `clear_run` of another run (no change). Keep every main-path test. Add no test that only proves a tool is installed.

**Files:**
- `scripts/production/staff.py` — the field, `set_run`, `clear_run`, the CLI.
- `scripts/production/test_staff.py` — rows for the field and both edits.
- `scripts/lib/unit_runs.py` — `open_run`, `held`, the removals.
- `scripts/lib/test_unit_runs.py` — mark tests become row tests.
- `scripts/direct/prepare_session.sh` — writer.
- `scripts/direct/end_session.sh` — clearer.
- `scripts/direct/test_end_session.py` — clear rows.
- `scripts/direct/progress_history.py` — writer at `start-run`.
- `scripts/direct/test_progress_history.py` — writer rows.
- `scripts/hooks/pre-tool-use-berth.py` — hook reader.
- `scripts/hooks/direct_run.py` — hook reader.
- `scripts/hooks/conversation_pause.py` — hook reader.
- `scripts/hooks/test_conversation_pause.py` — hook rows.
- `scripts/build_hold/build_hold.py` — reader.
- `scripts/build_hold/test_build_hold.py` — reader rows.
- `scripts/build_hold/override_guard.py` — drop the mark overrides.
- `scripts/build_hold/override_guard.sh` — drop the mark overrides.
- `scripts/build_hold/test_override_guard.py` — override rows.
- `scripts/production/worker.py` — reader.
- `scripts/production/test_worker.py` — reader rows.
- `scripts/production/session_staff.py` — reader.
- `scripts/production/test_session_staff.py` — reader rows.
- `scripts/production/unit_status.sh` — reader.
- `scripts/production/test_unit_status.py` — reader rows.
- `scripts/production/promote_showrunner.py` — reader.
- `scripts/production/test_promote_showrunner.py` — reader rows.
- `scripts/production/unit_lookup.py` — reader.
- `scripts/production/test_unit_lookup.py` — reader rows.
- `scripts/message/unit_labels.py` — reader.
- `scripts/message/test_unit_labels.py` — reader rows.
- `scripts/message/roster.py` — reader.
- `scripts/message/test_roster.py` — reader rows.
- `scripts/agents/shutdown.py` — reader.
- `scripts/agents/test_shutdown_record.py` — reader rows.
- `scripts/direct/assign.py` — reader.
- `scripts/shutdown/restart.py` — reader.
- `scripts/shutdown/inventory.py` — reader.
- `scripts/shutdown/shutdown.py` — reader.
- `scripts/herdr/worker_panes.py` — reader.
- `scripts/herdr/test_worker_panes.py` — reader rows.
- `scripts/usage/agents_log.py` — reader.
- `scripts/usage/test_agents_log.py` — reader rows.
- `scripts/lint/tmp_sweep.py` — reader.
- `scripts/lint/test_tmp_sweep.py` — reader rows.
- `commands/plan/shrink.md` — doc.
- `commands/unit/direct.md` — doc.
- `docs/as-built/hierarchy.md` — doc.
- `docs/as-built/session-notifier.md` — doc.
- `docs/as-built/session-roster.md` — doc.
- `docs/as-built/build-targets.md` — doc.
- `docs/as-built/merge-gate-and-test-speed.md` — doc.
- `docs/as-built/build-followups-fn-length-hook.md` — doc.
- `docs/plans/sdlc-devtools-path-copies.md` — doc.

**Workers:** 2 writers. The API in items 1 and 3 is fixed above, so both start at once.
- `impl` — items 1, 2, 3, 5, 7: `staff.py`, `unit_runs.py`, `prepare_session.sh`, `end_session.sh`, `progress_history.py`, the three hooks, their tests, and the deploy one-off.
- `test2` — items 4 (all readers not in `impl`'s list), 6 and their tests.

**State:** the staff record gains the `run` key. Old rows have no key and read as `NoRun`.

**Acceptance gate:** the targeted test is green; basedpyright reports 0 on the lint line; the grep in item 4 shows no reader of a mark folder.
