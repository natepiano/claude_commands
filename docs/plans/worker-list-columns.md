# Worker list columns

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `worker.py list` text rows show a Pane column (open or closed) and a State column (busy, idle, paused or exited, or `-` when the pane is not open). A closed worker shows no account.

> **As-built disposition: amend** — `docs/as-built/long-lived-workers.md` (worker list)

## Source

The user, through fastest-unit and the sdlc showrunner, 2026-11-09: "it doesn't need to output the account for a closed worker - that's pointless. It should not mix open, idle into a state row - there should be a Pane column stating whether it is open or closed and the State column should only reflect busy, idle, paused, exited but should show - if the pane is not open (because clearly it is not running)." Current output: `1 hana_catalyst-build-speed-tests-impl impl codex1 OpenWorker idle` and `2 hana_catalyst-build-speed-tests-test test account unknown ClosedWorker`.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-09T15:36:04.311+00:00
- **Stack:** Python 3.13, standard library only; Markdown.
- **Layout:** `scripts/production/` (staff and worker commands), `scripts/herdr/` (worker panes).
- **Key files:**
  - `scripts/production/worker.py` — `list_workers` (line 950) builds one dict for each observed worker; `list_all_workers` (line 920) builds the `--all` rows; `_print_workers` (line 965) prints the JSON or the text rows; `_worker_list_row` (line 779) adds name, id, role and account
  - `scripts/herdr/worker_panes.py` — `OpenWorker`, `StoppedWorker`, `ClosedWorker`, `UncertainWorker`, `ProcessUnknownWorker`; `worker_state` (line 487) gives `idle`, `busy` or `unknown`
  - `scripts/production/test_worker.py` — the list tests (near lines 2166, 2177, 2182 and 2257)
  - `commands/unit/worker.md` — line 11 describes the `list` output
- **Test lanes:** none (Python unittest modules beside the code).
- **Build:** none.
- **Test:** `scripts/lib/py -m unittest scripts.production.test_worker`, from the worktree root.
- **Lint:** `basedpyright <changed Python files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line; the exit status says nothing.
- **Invariants:**
  - Tests use fixtures; they never read live state (`~/.claude` main, `/tmp/claude`, live staff, live herdr).
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - All text uses Simplified Technical English.
  - One place for each thing: one function maps an observed worker to its pane and state words.

## Phases

### Phase 1 — Worker list text shows Pane and State columns  · status: done

**Depends on:** none

#### As-built

`worker.py list` text rows read `<n> <name> <role> <account> <pane> <state>[ <reason>]`. `_worker_pane_state` in `worker.py` is the one mapping from an observed worker to its pane and state words:

- `OpenWorker`: pane `open`, state `paused` or `exited` from the staff row, else `worker_panes.worker_state` (`busy`, `idle` or `unknown`).
- `StoppedWorker`: pane `open`, state `exited`.
- `ClosedWorker`: pane `closed`, state `-`.
- Any other observed state: pane `unknown`, state `-`, and the row keeps its reason.

A pane that is not open prints `-` for the account. `list --all` prints `-` for the pane, the account, and a state from the dispatch status. `list --json` and `list --json --all` keep their keys and values.

**Files:**
- `scripts/production/worker.py` — `_worker_pane_state`; `list_workers` adds the words for text output; `_print_workers` prints the columns
- `scripts/production/test_worker.py` — the expected text rows and the open, busy, stopped, closed, paused and uncertain rows
- `commands/unit/worker.md` — the `list` description

**Gotchas:** `list_workers(as_json=False)` replaces the `state` key with the column word, so the key holds the observed class name only in JSON output.

### Phase 2 — Status has no machine word; a Machine column shows only off natedev  · status: todo

**Depends on:** Phase 1

**Source:** The user, through natedev and the sdlc showrunner, 2026-11-09: "there should be no such thing as a status of "on mac". If you need to specify the machine, i.e. any time something runs other than on natedev, add a column for that. If they are all on natedev, don't show a column." The showrunner: Status stays busy, idle, paused or exited. The same rule holds for the accounts board. Fix it where the status is made, so the tables and `worker.py list` take it from one place.

#### Work Order

**Goal:** No status reads `on mac`. Each status keeps its status word. A table that lists sessions shows a Machine column only when at least one of its rows runs off natedev.

**Spec:**

1. Remove the `on {machine}` status at `scripts/production/live_units.py:86`. The status stays the word that `session_state.classify` gives.
2. Add one function that takes the machine of each row and decides the Machine column. It shows the column only when a row is off natedev. Read the name of the home machine from existing config (`config/build_targets.conf`); add no literal.
3. `LiveUnit` carries the machine of its staff row. `unit_phases.table` and `style_units.table` print the Machine column through the function in item 2.
4. `worker.py list` text prints the Machine column through the same function, after the account column. A worker's machine is the machine of its parent's worktree. `list --json` stays unchanged.
5. Find each other place that prints `on <machine>` as a status, in `scripts/accounts/` and `mods/unit-board/`. Give each the same column rule. If the accounts board has no such place, say so in the summary.
6. Tests: add rows to the existing table tests for all rows on natedev (no column) and one row on the Mac (column shown). Change each existing expected `on mac` value. Add no new test file.
7. Update each doc that describes the `on <machine>` status.

**Files:**
- `scripts/production/live_units.py` — the status; the machine on `LiveUnit`
- `scripts/production/unit_phases.py`, `scripts/production/style_units.py` — the column
- `scripts/production/worker.py` — the column
- `scripts/production/display_cells.py` — the one column function
- `scripts/production/test_live_units.py`, `scripts/production/test_unit_phases.py`, `scripts/production/test_style_units.py`, `scripts/production/test_worker.py` — the tests
- `scripts/accounts/accounts.py`, `scripts/accounts/test_accounts.py`, `mods/unit-board/` — only where Spec item 5 finds an `on <machine>` status
- `docs/as-built/unit-board.md` — the status text

**Workers:** 1 writer — one function and its callers; nothing splits.
- `impl` — all Files above.

**State:** none — the phase changes text output only.

**Constraints from prior phases:** The Phase 1 text row order is `<n> <name> <role> <account> <pane> <state>[ <reason>]`.

**Acceptance gate:**
- The changed test modules pass, and `basedpyright` on the changed Python files gives `0 errors, 0 warnings, 0 notes`.
- With every row on natedev, no table has a Machine column.
- With one row on the Mac, the table has a Machine column and that row's status is a status word.
