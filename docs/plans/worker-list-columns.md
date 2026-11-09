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

### Phase 2 — Status has no machine word; a Machine column shows only off natedev  · status: done

**Depends on:** Phase 1

#### As-built

`session_state.classify` gives the one status word: `busy`, `idle`, `paused` or `exited`. `LiveUnit.status` holds that word in the unit and style tables. No table prints `on <machine>` as a status. The accounts board and `mods/unit-board/` have no such status.

`display_cells.machine_column(machines: list[str]) -> tuple[str, ...]` is the one Machine rule. It returns one cell for each row when a row uses a machine other than the home machine, else an empty tuple. The home machine is the sole `kind.<machine>=local` entry in `config/build_targets.conf`. Any other number of such entries raises `conf.Unusable`, which `worker.py` reports as an anomaly.

`staff.worktree_machine(row)` reads the machine of a present or removed worktree, else `""`. `LiveUnit.machine` holds it, and a worker uses the machine of its parent's worktree. `unit_phases.table` and `style_units.table` print a `Machine` column after Status only when the cells are not empty. `worker.py list` text prints an optional machine field after the account. `list --json` keeps its keys and values.

**Files:**
- `scripts/production/display_cells.py` — `machine_column`
- `scripts/production/staff.py` — `worktree_machine`
- `scripts/production/live_units.py` — `LiveUnit.status` and `LiveUnit.machine`
- `scripts/production/unit_phases.py`, `scripts/production/style_units.py`, `scripts/production/worker.py` — print the column
- `scripts/production/test_unit_phases.py`, `scripts/production/test_style_units.py`, `scripts/production/test_worker.py`, `scripts/style/test_style_plan_split.py` — the expected rows
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/unit-board.md` — the status words and the column

**Gotchas:** `UnitPhasesTests.setUp` fixes `socket.gethostname` to `natedev`. The fixture records the host name as the machine, and the merge gate runs on a cloud box with a different host name.
