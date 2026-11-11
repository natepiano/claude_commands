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

**Depends on:** none

#### Work Order

**Goal:** Rows sort by unit name; Progress is a colored bar only; Phase shows `<done>/<total>`; a new Notes column holds every other phase text.

**Spec:**

1. **Sort.** Both tables (phase and style) list rows by unit name, case-insensitive. Sort once where `main` gets the unit list, before either table is built, so the Machine column index stays aligned.
2. **Progress.** Only the bar, with no text after it.
   - `🟩` done, `🟨` running (open), `⬜` not started (todo).
   - Keep the scaling rule: at most 20 cells; cell `i` covers phases `[⌊i·n/20⌋, ⌊(i+1)·n/20⌋)` and shows running, else not started, else done.
   - No phases gives `—`.
   - All three characters have East Asian Width `W`, so each takes two columns and the Markdown table stays aligned. Do not mix in a narrow character.
3. **Phase.** `<done>/<total>` when the plan has phases. Otherwise `—`.
4. **Notes.** A new column directly after Phase. Parts join with ` · `, cut to 80 characters with `…`:
   - An open phase: `phase <id> — <step>: <task>`, or `phase <id> — <title>` with no open step (the old Phase text without `of <total>`).
   - No open phase: `between phases`, `not started`, `plan done`, `no phases`, or `no plan`, as Phase showed them before. `between phases` drops `, <done> of <total> done`, which Phase now shows.
   - Fix rounds: `fix rounds <n>`, plus ` + 1 running` when one runs. This moves out of Progress.
   - Nothing to say gives `—`.
5. The header becomes `| Unit | Status | [Machine |] Progress | Phase | Notes | ETA | Workers | Non-Phase Work | Queued |`.
6. Update `commands/showrunner/units.md` and the two as-built docs to state the new columns, in the same commit.

**Files:**
- `scripts/production/unit_phases.py` — sort, `progress_cell`, `phase_cell`, a new `notes_cell`, `table` header and cells.
- `scripts/production/test_unit_phases.py` — rows for each rule above.
- `commands/showrunner/units.md` — column text.
- `docs/as-built/showrunner-automation.md`, `docs/as-built/phase-tables.md` — column text.

**Workers:** `1 writer + 1 tester`
- `impl` — `unit_phases.py`, `units.md`, both as-built docs.
- `test` — `test_unit_phases.py`: change the existing table rows to the new layout, and add rows for sorting, the three bar characters, the Phase count, and each Notes form.

**State:** none

**Constraints from prior phases:** none

**Acceptance gate:**
1. `basedpyright scripts/production/unit_phases.py scripts/production/test_unit_phases.py`: 0 errors, 0 warnings, 0 notes.
2. `env HERDR_BIN=/nonexistent/herdr PYTHONPATH="$PWD" scripts/lib/py -m unittest scripts.production.test_unit_phases scripts.production.test_roles_audit` passes.
3. Live: `PYTHONPATH="$PWD" scripts/lib/py scripts/production/unit_phases.py --production sdlc` prints sorted rows and colored bars.

### Phase 2 — ETA Next and ETA Plan  · status: todo

**Depends on:** Phase 1

#### Work Order

**Goal:** ETA becomes ETA Next, the promise for what comes next; a new ETA Plan column shows the plan finish, never earlier than a promise inside the plan.

**Spec:**

1. **ETA Next** replaces ETA, in the same place. It shows the latest stated promise of the first of these that exists:
   1. the open plan phase;
   2. the newest open phase outside the plan (the work in Non-Phase Work);
   3. the next not-started plan phase that has a promise (the checkpoint ETA for the next phase).
   - A passed promise adds ` · late <elapsed>`.
   - Open work with no promise shows `no ETA`.
   - Nothing open and no promise gives `—`.
   - Time forms stay: `HH:MM` the same day, `Sat 18:41` within six days, otherwise `24 Oct`.
2. **ETA Plan** comes directly after ETA Next. It shows the plan's predicted finish (`PredictedFinish`), with the same time forms and no `ends` word.
   - The column shows only when at least one row has a plan with phases that are not all done. Otherwise the table has no ETA Plan column.
   - A row with no running plan shows `—`.
3. **Plan finish never earlier than a promise.** Fix this in `phase_table._plan_finish`, where the defect lives: add the latest stated promise (`eta_trail[-1][0]`) of every open and not-started phase to the projections before taking the maximum. Seen live: the phases unit showed `ends 15:42` while its phase 5 had a promise of 11-15 18:00. Every reader of `plan_finish` (the unit report too) gets the corrected time.
4. The `ends <time>` part leaves ETA Next.
5. Update `commands/showrunner/units.md` and the two as-built docs in the same commit.

**Files:**
- `scripts/production/unit_phases.py` — `eta_cell` becomes the ETA Next cell, a new plan-finish cell, the optional column in `table`.
- `scripts/direct/phase_table.py` — `_plan_finish`.
- `scripts/production/test_unit_phases.py`, `scripts/direct/test_phase_table.py` — rows for each rule.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/phase-tables.md` — column text.

**Workers:** `1 writer + 1 tester`
- `impl` — `unit_phases.py`, `phase_table.py`, `units.md`, both as-built docs.
- `test` — `test_unit_phases.py` rows for each ETA Next source, the hidden and shown ETA Plan column; a `test_phase_table.py` row where a not-started phase's promise is later than the predicted finish.

**State:** none

**Constraints from prior phases:** Phase 1 sets the column order up to Notes. ETA Next and ETA Plan follow Notes.

**Acceptance gate:**
1. `basedpyright scripts/production/unit_phases.py scripts/production/test_unit_phases.py scripts/direct/phase_table.py scripts/direct/test_phase_table.py`: 0 errors, 0 warnings, 0 notes.
2. `env HERDR_BIN=/nonexistent/herdr PYTHONPATH="$PWD" scripts/lib/py -m unittest scripts.production.test_unit_phases scripts.direct.test_phase_table scripts.production.test_roles_audit` passes.
3. Live: the phases row shows an ETA Plan no earlier than its phase 5 promise.
