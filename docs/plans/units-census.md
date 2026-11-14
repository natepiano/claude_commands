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

#### As-built

- `scripts/production/census.py` runs `cargo-handler census --json` once per run within `units_census_seconds`. `parse` accepts schema 1 and 2 and returns `Census(machines, directors, workers)` of typed records (`CensusMachine`, `CensusDirector`, `CensusWorker`, `CensusRun`, `CensusPass` or `NoPass`). A time-out, a failed run, unreadable output, bad JSON, another schema or a bad field returns `CensusFailed(reason)`.
- `unit_phases.main` submits `census.read` first in its thread pool. `table()` takes `census_read: Census | CensusFailed` and has no `pane_rows` argument.
- Status (`census_status`): the census director's `status`, or `—` when it is null. A unit that the census does not list shows `exited` only when its machine (`unit.machine` or the local machine) is a reachable census machine; otherwise `—`.
- Workers (`census_workers`, `workers_cell`): a phase row lists the census workers whose `pass.phase` is that phase's instance id; another row lists all workers of the unit. `running` → `<name> waiting on run: <label> <elapsed>`, `finished_queued` → `<name> result queued: <label>`, `missing` → `<name> run gone: <label>`. A busy or starting worker without a run shows `<name> working`; a waiting one shows `<name> waiting`; idle workers add `+<n> idle`.
- A census failure shows `—` in Status and Workers for every unit and sends one anomaly line through `session_staff.report_anomaly`. There is no fallback to pane observation.
- `scripts/direct/worker_runs.py` is now `scripts/direct/phase_waits.py` and keeps only `phase_waits` for Notes.

**Files:**
- `scripts/production/census.py` — the census reader and its records.
- `scripts/production/unit_phases.py` — Status and Workers from the census.
- `scripts/direct/phase_waits.py` — the Notes phase waits.
- `config/production.conf` — `units_census_seconds`.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/staff-accessors.md` — the rules.

**Binds later work:** `census.parse` is the place that reads the schema 2 `sessions` list. `unit_phases.census_status` is the Status lookup that schema 2 can replace.

**Gotchas:**
- One census takes 2.6 s to 3.8 s on natedev, and more than 10 s under heavy load. It is the critical path of `/showrunner:units`, so the land waits for the handler census speed fix (target about 1 s).
- `pass.phase` is the phase instance id, not the plan phase number.
- A test that builds the table without a census read reads live data; every test injects a `Census`.

### Phase 2 — Take census schema 2  · status: todo

#### Work Order

**Goal:** the reader takes schema 2 when the handler sends its hash (handler Phase 10, ETA 22:30 EST).

**Spec:**
1. `census.parse` reads the top-level `sessions` list into a typed record.
2. Use it where it replaces a staff lookup in `unit_phases.census_status`.
3. Drop schema 1 support once the installed handler prints schema 2.
4. Tests inject census records as in phase 1. Update the docs in the same commit.

**Files:**
- `scripts/production/census.py`
- `scripts/production/test_census.py`
- `scripts/production/unit_phases.py`
- `scripts/production/test_unit_phases.py`
- `commands/showrunner/units.md`
- `docs/as-built/showrunner-automation.md`
