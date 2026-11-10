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

#### As-built

- `staff.py` holds `OpenRun(path: Path)`, `NoRun` and `StaffRow.run: OpenRun | NoRun`. The JSON key `run` is a path string; an absent key reads as `NoRun`.
- `staff.set_run(identity, run)` and `staff.clear_run(identity, run)` go through the row transaction. `clear_run` clears only while the row still names that run (resolved paths). The CLI is `staff.py set-run|clear-run <session id> <run folder>`; no row prints one line and exits 1.
- `prepare_session.sh` and `progress_history.py start-run` set the pointer. `end_session.sh` and `finish-run` clear it, for every row kind.
- `unit_runs.open_run(session_id)` returns the run of the row whose `current_session` is that session. `unit_runs.held` reads it. The mark folders `/tmp/claude/unit/active` and `/tmp/claude/delegate/active`, the `active_marker` files, the mark API and the `UNIT_ACTIVE_DIR` and `PLAN_DELEGATE_ACTIVE_DIR` overrides are gone.
- Every reader reads the row: hooks, build hold, worker list, staff, roster, unit labels, unit status, shutdown, inventory, restart, worker panes, agents log and the tmp sweep. Each hook makes one staff read per call.
- A one-off outside the repo copies live marks into rows at deploy, then deletes both mark folders: `/tmp/claude/scratch/until-2026-11-18T0500Z-run-pointer-deploy-5fcad7/migrate_run_pointers.py --checkout <checkout>`. It keeps each mark whose copy failed and exits nonzero.

**Files:**
- `scripts/production/staff.py` — the field, `set_run`, `clear_run`, the CLI.
- `scripts/lib/unit_runs.py` — `open_run`, `held`.
- `scripts/direct/prepare_session.sh`, `scripts/direct/end_session.sh` — set and clear the pointer.
- `scripts/direct/progress_history.py` — sets the pointer at `start-run` and on reopen; clears it at `finish-run`.
- `scripts/hooks/conversation_pause.py`, `scripts/hooks/user-prompt-submit-conversation-pause.py`, `scripts/hooks/direct_run.py`, `scripts/hooks/pre-tool-use-berth.py` — hook readers.
- `scripts/agents/shutdown.py`, `scripts/agents/inventory.py` — shutdown readers.
- `scripts/build_hold/build_hold.py`, `scripts/build_hold/override_guard.py`, `scripts/build_hold/override_guard.sh` — build hold reader; no mark overrides.

**Gotchas:**
- `prepare_session.sh` continues when the session has no staff row, but exits on an unreadable staff record.
- The reopen path publishes the pointer before it writes `active`, so a rerun after a failed write publishes it.
- The user-prompt hook reads staff once and passes the record to activity, holds and timers.
- The stop hook allows the stop with no output when staff is unreadable.
- Tests use their own `STAFF_FILE` and run root, never live staff or live units.
