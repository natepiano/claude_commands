# Units table layout

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The `/showrunner:units` phase table gets sorted rows, a colored bar, a Notes column and two ETA columns.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`, `docs/as-built/phase-tables.md`

The user's words, from the sdlc showrunner on 2026-11-11:

1. "Unit column should be alphabetical"
2. "we should use colors on the bar to say what is done, running, not started"
3. "the n/m for phases should be in the Phase column not in progress - the progress can just be a bar with no text"
4. "i want to use the same busy/idle/paused/exited that we always use it has to be its own thing - we need a different column explaining any status notes then"
5. "ETA Next - should be for whatever is coming next whether that's the current phase or just promised work. ETA Plan should be a column that we show if we're running a plan still"

## Delegation Context

- **Project:** the `~/.claude` scripts — `/showrunner:units` prints one row per production unit.
- **Project started:** 2026-11-11T17:42:04.619+00:00
- **Stack:** Python 3.13, basedpyright, `unittest`.
- **Layout:**
  - `scripts/production/unit_phases.py` — builds both tables (phase and style) and prints them.
  - `scripts/production/test_unit_phases.py` — its tests.
  - `scripts/direct/phase_table.py` — builds `PhaseRecord`, including `plan_finish` (`_plan_finish`, about line 920).
  - `scripts/direct/test_phase_table.py` — its tests.
  - `commands/showrunner/units.md` — the command text that states each column.
  - `docs/as-built/showrunner-automation.md` (the "Phase productions" item, about line 632) and `docs/as-built/phase-tables.md` — as-built text for the table.
- **Key files:** as in Layout.
- **Test lanes:** `scripts/production/test_unit_phases.py`, `scripts/direct/test_phase_table.py`.
- **Build:** `basedpyright <changed files>` must read `0 errors, 0 warnings, 0 notes`.
- **Test:** `env HERDR_BIN=/nonexistent/herdr PYTHONPATH="$PWD" scripts/lib/py -m unittest <modules>`.
- **Lint:** none beyond basedpyright.
- **Invariants:**
  - Status keeps the shared words `busy`, `idle`, `paused`, `exited`. No other text goes into Status.
  - The optional Machine column stays directly after Status.
  - Display names come from `config/names.toml` (`worker_heading`); `test_roles_audit` fails on a fixed name.
  - The table prints as Markdown inside a Claude reply. ANSI colors do not show there.
  - Each cell keeps the 80-character cut with `…`, except the bar.

## Phases

### Phase 1 — Sorted rows, a colored bar, and a Notes column  · status: done

#### As-built

- `main` sorts the unit list by unit name, case-insensitive, before it builds the phase and style tables. The Machine column index stays aligned.
- `progress_cell` shows only a bar: `🟩` done, `🟨` running, `⬜` not started. It has at most 20 cells; cell `i` covers phases `[⌊i·n/20⌋, ⌊(i+1)·n/20⌋)`. No phases gives `—`.
- `phase_cell` shows `<done>/<total>` from the same phase rows as Progress, else `—`.
- `notes_cell` holds the open phase (`phase <id> — <step>: <task>` or `phase <id> — <title>`), else `between phases`, `not started`, `plan done`, `no phases` or `no plan`, plus `fix rounds <n>[ + 1 running]`. Parts join with ` · ` and cut at 80 characters.
- Progress, Phase and Notes read `progress_record`: the open-run record from `phase_table.build_run` when the plan record has no phases. Notes reads the plan record when that run record has no phases either, so an unreadable plan still shows `no plan`.
- The header is `| Unit | Status | [Machine |] Progress | Phase | Notes | ETA | Workers | Non-Phase Work | Queued |`.

**Files:**
- `scripts/production/unit_phases.py` — the sort, `progress_cell`, `phase_cell`, `notes_cell`, and the `table` header and cells.
- `scripts/production/test_unit_phases.py` — layout, sort, bar, Phase and Notes rows.
- `scripts/agents/test_shutdown_cli.py` — pins the units table header.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/phase-tables.md` — column text.

**Binds later work:** ETA columns follow Notes and read `progress_record`, as Progress, Phase and Notes do.

**Gotchas:** `scripts/agents/test_shutdown_cli.py` pins the units header; each header change updates it. The three bar characters have East Asian Width `W`; a narrow character breaks the Markdown alignment.

### Phase 2 — ETA Next and ETA Plan  · status: done

**Depends on:** Phase 1

#### As-built

- `eta_cell` (ETA Next) shows the latest promise of the first of: the open plan phase (from the open run's instances when present), the newest open phase outside the plan, the current open phase, the first not-started plan phase with a promise. A passed promise adds ` · late <elapsed>`. Open work with no promise gives `no ETA`; otherwise `—`. The `ends <time>` part is gone.
- `eta_plan_cell` (ETA Plan) shows the `PredictedFinish` only when `running_plan` (some plan phase is not done), else `—`. `table` shows the ETA Plan column only when a row has a running plan.
- `phase_table._plan_finish` adds `eta_trail[-1][0]` of every `OpenPhase` and `TodoPhase` to the projections, so the plan finish is never earlier than a phase promise. Every `plan_finish` reader gets this time.
- `phase_table._phase_history` attaches an `eta_stated` event whose `eta_phase_id` names a row with no instances. It skips an event that `_instance_eta_events` gives to an earlier instance with that number, or that was stated before that instance's recorded finish.
- The header is `| Unit | Status | [Machine |] Progress | Phase | Notes | ETA Next | [ETA Plan |] Workers | Non-Phase Work | Queued |`.

**Files:**
- `scripts/production/unit_phases.py` — `eta_cell`, `running_plan`, `eta_plan_cell`, the `table` header and cells.
- `scripts/direct/phase_table.py` — `_plan_finish`, `_phase_history`.
- `scripts/production/test_unit_phases.py`, `scripts/direct/test_phase_table.py`, `scripts/agents/test_shutdown_cli.py` — rows and header.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/phase-tables.md` — column text.

**Gotchas:** `build_plan` shows an unfinished plan phase as `TodoPhase` when outside work opens after it; ETA Next reads the open run's instances for that reason. A run that closes without `phase_finished` leaves `PhaseInstanceWithoutFinish`; it does not block a later promise for the same phase number.
