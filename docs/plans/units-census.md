# Units view: Status and Workers read the cargo-handler census

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `/showrunner:units` takes Status and Workers from `cargo-handler census --json`, so the units table and the handler tiles use one source. The `worker_runs` pane observation for the table retires.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`

The showrunner's order on 2026-11-13 19:4x EST: "Start the switch phase now: the units table Status and Workers read the census (one source for units and handler), and the worker_runs observation for the table retires. Schema: ~/rust/cargo-liner/docs/cargo-handler/as-built/hierarchy.md. Schema 2 adds a top-level sessions list (handler Phase 10, ETA 22:30 EST); build on schema 1 now and take schema 2 when its hash comes."

## Facts measured on natedev, 19:51 EST

- `cargo-handler census --json` (installed from cargo-liner main d20fee981) prints schema 1: `machines`, `directors`, `workers`. Each director has `staff_id`, `name`, `machine`, `current_run_id`, `status`, `activity`, `activity_started_at`, `unknown`. Each worker adds `parent_staff_id`, `pass {role, phase}`, `pane`, and `runs [{label, started_at, state, unknown}]`.
- The census lists a director or worker only while a handler tile shows its session. A worker must also be in its unit's current run.
- `status` is the accounts command's word, or null when the accounts command has no entry.
- One census takes 3.2 s to 4.8 s. `unit_phases.py --production sdlc` takes 1.5 s in total today.
- `~/.config/cargo-handler/config.toml` has `remote = []`, so the census lists no Mac sessions.

## Delegation Context

- **Project:** the `~/.claude` scripts — the `/showrunner:units` view (`scripts/production/unit_phases.py`).
- **Project started:** 2026-11-14T00:53:17.156+00:00
- **Stack:** Python 3.13, basedpyright, `unittest`. `cargo-handler census --json` (cargo-liner main d20fee981) supplies the census.
- **Layout:**
  - `scripts/production/unit_phases.py` — `table()` builds table 1 with Status and Workers; `workers_cell()` writes the Workers text; `main()` runs the reads in a thread pool.
  - `scripts/production/live_units.py` — `read_units()` supplies the units and today's Status.
  - `scripts/direct/worker_runs.py` — `observe_pane_rows()`, `phase_workers()`, `pane_worker_rows()` observe worker panes and run records for the table today.
  - `~/rust/cargo-liner/crates/cargo-handler/README.md` section `census` — schema 1 and its example.
  - `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md` — the units rules.
- **Test lane:** `scripts.production.test_unit_phases`, `scripts.production.test_display_cells`, `scripts.direct.test_phase_waits`, `scripts.production.test_census`, `scripts.production.test_staff_accessors`, `scripts.agents.test_shutdown_cli`, `scripts.accounts.test_accounts`.
- **Build:** `basedpyright` on each changed Python file must read `0 errors, 0 warnings, 0 notes`.
- **Test:** `env HERDR_BIN=/nonexistent/herdr PYTHONPATH="$PWD" scripts/lib/py -m unittest` with the test lane modules.
- **Invariants:**
  - No test runs `cargo-handler` or reads a live pane.
  - The style production table (`style_units.py`) does not change.
  - Tunables go in `config/production.conf`.

## Phases

### Phase 1 — Status and Workers read the census (schema 1)  · status: done

#### Work Order

**Goal:** `/showrunner:units` shows Status and Workers from one `cargo-handler census --json` read. The table no longer observes worker panes or run records for these cells.

**Spec:**
1. **Reader.** New `scripts/production/census.py` runs `cargo-handler census --json` once per run, with the time limit `units_census_seconds` in `config/production.conf`. It parses schema 1 into typed records (`CensusDirector`, `CensusWorker`, `CensusRun`) and accepts schema 1 and 2. A failed run, a time-out, bad JSON or another schema returns `CensusFailed(reason)`.
2. **Overlap.** `unit_phases.main` starts the census in its existing thread pool at the start of the reads, so its time overlaps the staff, plan and history reads. Name the measured total in the phase report.
3. **Status.** The unit's Status is the census director's `status` for its staff id. A unit that the census does not list shows `exited`. A null status shows `—`. Status keeps its words: `busy`, `starting`, `waiting`, `idle`, `paused`, `exited`.
4. **Workers.** The unit's Workers cell lists the census workers whose `parent_staff_id` is the unit. A phase row lists only workers whose `pass.phase` is that phase. Run states map to the current text: `running` → `<name> waiting on run: <label> <elapsed>`, `finished_queued` → `<name> result queued: <label>`, `missing` → `<name> run gone: <label>`. A busy worker without a run shows `<name> working`. Idle workers add `+<n> idle`.
5. **Failure.** When the census fails, Status and Workers show `—` for every unit, and the run prints one anomaly line with the reason. No fallback to the old observation.
6. **Retire.** Remove the table's use of `worker_runs.observe_pane_rows`, `worker_runs.phase_workers`, `worker_runs.pane_worker_rows` and the `pane_rows` argument. Delete each function that has no other consumer. Keep `phase_waits` for Notes.
7. **Tests** inject census JSON. No test runs `cargo-handler` or reads a live pane.
8. **Docs.** Update `commands/showrunner/units.md` and the units section of `docs/as-built/showrunner-automation.md` in the same commit.

**Files:** `scripts/production/census.py` (new), `scripts/production/unit_phases.py`, `scripts/direct/worker_runs.py` (renamed `phase_waits.py`), `config/production.conf`, `docs/as-built/staff-accessors.md`, their tests, `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md`.

**Acceptance:** the sdlc table shows the same Status words as the handler tiles for every listed unit. A census failure shows `—` and one anomaly line. The full production test modules pass. basedpyright reports 0/0/0.

### Phase 2 — Take census schema 2  · status: todo

#### Work Order

**Goal:** the reader takes schema 2 when the handler sends its hash (handler Phase 10, ETA 22:30 EST).

**Spec:** read the top-level `sessions` list into a typed record. Use it where it replaces a staff lookup in Status. Drop schema 1 support once the installed handler prints schema 2. Tests and docs as in phase 1.
