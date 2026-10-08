# phase-tables

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Each production unit keeps one Markdown note in the vault with its current phase, its ETA and a table of every phase's start and finish; the dailies chart reads the same record.

> **Production: build-followups** — unit `phase-tables-unit`; production doc `docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` (natepiano/claude_commands) — the commands, docs and scripts every Claude and Codex session runs. Work only in the worktree `/home/natepiano/worktrees/claude-build-followups-phase-tables`, branch `build-followups-phase-tables`.
- **Project started:** 2026-10-08T15:33:07.973+00:00
- **Stack:** Python 3.10+ (stdlib only, `unittest`), zsh, Markdown command files. basedpyright must report 0 errors and 0 warnings; no file-level ignores; no `Any`.
- **Layout:** `scripts/delegate/` (the progress recorder and this plan's script), `scripts/production/` (showrunner scripts), `commands/unit/`, `commands/showrunner/`, `docs/`.
- **Key files:**
  - `scripts/delegate/progress_history.py` — the recorder. Session state in `<session-dir>/progress_history_state.json`; durable events in `~/.local/state/plan-delegate/runs/<run-id>.jsonl` (`_history_root()`, env override). Plan headings: `PHASE_HEADING_PATTERN`, `PHASE_STATUS_PATTERN`, `PHASE_TITLE_PATTERN`, `_count_plan_phases` (line 538). ETA maths: `_eta_seconds` (1910), `_eta_band_cells` (3408), `_percent_spread` (3445), `_recorded_report` (3776). Transitions: `_start_phase` (1169), `_progress` (3883), `_finish_phase` (4121), `_finish_run` (4147). Run lookup: `_run_started_event` (703). Event append: `_append_event` (370). Clock: `_now_epoch` (honours `PLAN_DELEGATE_NOW_EPOCH`).
  - `scripts/delegate/test_progress_history.py` — its tests; they run the recorder in temp dirs.
  - `scripts/delegate/findings.py` — precedent for a second script reading `progress_history_state.json`.
  - `scripts/production/add_unit.py` — `read_production(path) -> Production` (`slug`, `showrunner_session`, `zone`), `production_field`, `cell_value`, the Units table readers.
  - `scripts/whoami/agent_notes.py` — precedent for a script writing vault notes: temp file, keep mode, `os.replace` (`set_fields`, line 81).
  - `commands/unit/report.md` — `<ProgressReport/>`, read at every tick. `commands/unit/eta.md`, `commands/unit/eta_breakdown.md` — where a unit states an ETA.
  - `scripts/production/dailies_input.py` — the dailies input builder (`eta_state` 213, `eta_value` 244, `run` 292). `commands/showrunner/dailies.md` — its input contract.
  - `docs/as-built/plan-delegate-progress-history.md` — the recorder's as-built.
- **Test lanes:** `scripts/delegate/` and `scripts/production/` — tests sit beside the code as `test_<module>.py` (`unittest`).
- **Build:** none (Python).
- **Test:** `python3 -m unittest discover -s scripts/delegate -p 'test_phase_table.py'` and `python3 -m unittest discover -s scripts/delegate -p 'test_progress_history.py'`; Phase 5 adds `python3 -m unittest discover -s scripts/production -p 'test_dailies_input.py'`.
- **Lint:** `basedpyright <each changed .py>` — pass is its `0 errors, 0 warnings` line (it exits 3: pyrightconfig names a `.venv` no checkout has).
- **Invariants:**
  - The table writer never costs a report: no failure in it changes the recorder's output or exit status (user: agents must not re-read or re-write the note, and a report must not break).
  - No session name is stored. A unit is keyed by production slug and unit id; a name is only ever a file's name, recomputed at each write (the user's lookup ruling, 2026-10-08, relayed by the showrunner).
  - Scripts own the note's format. No command file tells an agent to write or edit the note.
  - Tests never write to the real vault or state directory: every root has an env override, and tests set it.
  - Every time shown is in the production doc's **User zone**.
  - Forbidden-words hooks apply to code, comments and prose.
  - Do not edit `commands/unit/delegate.md` or `scripts/delegate/verify.sh`: build-report-unit's unmerged branch changes both (the showrunner, 2026-10-08). If a phase cannot avoid one, tell the showrunner before editing.
  - Gate G2 is registered in the production doc; the showrunner says when it clears. Nothing is built for the Mac until the user answers (the showrunner, 2026-10-08).

## Phases

### Phase 1 — The phase table, built from the run's records  · status: done

#### Work Order

**Goal:** `phase_table.py show --session-dir <dir>` prints a unit's current phase and a table of every phase's start and finish, for any live delegate run, with no agent input.

**Spec:**

New `scripts/delegate/phase_table.py`, importing `progress_history` as a module. It reads; it appends no event.

1. In `progress_history.py`, add public functions and leave callers' behaviour unchanged:
   - `plan_phases(plan_path: Path) -> list[PlanPhase]`, `PlanPhase(TypedDict)`: `id: str`, `title: str`, `done: bool`, in document order. Same classification as `_count_plan_phases` (a heading with no status marker is done; `### Phase 12 Review` is skipped; a duplicate id is skipped). `title` is the heading text after the dash, with any `· status: …` marker and trailing commit annotation in parentheses removed. `_count_plan_phases` now counts over `plan_phases` so the two cannot disagree.
   - `eta_band_seconds(percent: int, elapsed: int, spread: float) -> tuple[int, int, int] | None` — `(eta, low, high)` seconds remaining; `_eta_band_cells` formats from it.
   - `plan_runs(plan_path: Path) -> list[Path]` — every run file under `_history_root()/runs` whose `run_started` event names this plan (resolve the event's `plan_doc` against its `working_dir` with `_plan_path`), oldest first.
2. `build(session_dir: Path) -> PhaseRecord` in `phase_table.py`:
   - Read the state file; plan path = `_plan_path(working_dir, project_plan_doc or plan_doc)`. No plan: raise `NoPlan`.
   - Per plan phase id, from `phase_started` / `phase_finished` events across `plan_runs`: `start` = earliest `phase_started`; `finish` = the `phase_finished` whose status is `completed`; `seconds` = the sum of `phase_elapsed_seconds` over that id's finished instances. A done phase with no events keeps `start`, `finish`, `seconds` as `None`.
   - Current phase = the state's `phase` when its status is `active`. `percent` and the projection come from that phase's last `progress_reported` event (`phase_percent`, the event time, the elapsed at that time, `_percent_spread` of its calibration): `eta`, `earliest`, `latest` = event time + `eta_band_seconds`. No report yet, or percent 0: all three `None`.
   - Prediction for each todo phase after the current one: `typical` = median `seconds` of this plan's completed phases; with none completed, the current phase's projected total (start to `eta`); with neither, no prediction. `gap` = median seconds from a completed phase's finish to the next phase's start within one run, 0 with no sample. Chain: start = previous finish + `gap`, finish = start + `typical`. `plan_finish` = the last phase's finish.
   - `PhaseRecord` and its rows are `TypedDict`s; every time is an aware `datetime` internally.
3. `render(record: PhaseRecord, zone: ZoneInfo) -> str` — Markdown, exactly:

   ```
   **Phase 3 of 6 — <title>**

   | | |
   | --- | --- |
   | Started | 10-08 09:12 |
   | Done | 60% |
   | ETA | 10-08 10:40 (10:25 to 11:05) |
   | Plan finish | 10-08 15:10, predicted |
   | Updated | 10-08 09:55 PDT |

   | Phase | What it delivers | Status | Start | Finish |
   | --- | --- | --- | --- | --- |
   | 6 | <title> | predicted | 10-08 13:40 | 10-08 15:10 |
   | 3 | <title> | running, 60% | 10-08 09:12 | 10-08 10:40 |
   | 2 | <title> | done in 0:55 | 10-08 08:10 | 10-08 09:05 |
   ```

   Rows are in descending plan order, so the oldest phase is last. Every time is `MM-DD HH:MM` in `zone`; the zone abbreviation appears once, on `Updated`. An unknown cell is `—`. With no active phase the heading is `**No phase running — <done> of <total> done**` and the ETA row is omitted. `Updated` is `_now_epoch()`.
4. CLI: `phase_table.py show --session-dir <dir> [--zone <IANA>] [--json]`. Zone defaults to the machine's. `--json` prints the record: ISO-8601 times with offset, keys `plan`, `updated`, `plan_finish`, `current` (`phase`, `of`, `title`, `started`, `percent`, `eta` as `{time, earliest, latest, source: "projected"}` or `null`) and `phases` (`phase`, `title`, `status` ∈ `done|running|todo`, `start`, `finish`, `seconds`). Exit 1 with one line when the session has no state or no plan.

**Files:**
- `scripts/delegate/phase_table.py` — new: `build`, `render`, `show`.
- `scripts/delegate/test_phase_table.py` — new.
- `scripts/delegate/progress_history.py` — `plan_phases`, `eta_band_seconds`, `plan_runs`; `_count_plan_phases` and `_eta_band_cells` built on them.
- `scripts/delegate/test_progress_history.py` — cases for the three new functions.

**Seats:** 1 writer + 1 tester — the Spec fixes the record and the Markdown, so tests are written from it.
- `impl` — `scripts/delegate/phase_table.py`, `scripts/delegate/progress_history.py`
- `test` — `scripts/delegate/test_phase_table.py` and the new cases in `scripts/delegate/test_progress_history.py`: fixtures are a temp history root, a plan file with all three heading forms, and event lines; they cover a phase run twice across two runs, a done phase with no events, prediction with and without a completed phase, the no-active-phase form, and the exact Markdown.

**Constraints from prior phases:** none.

**Acceptance gate:** both Test commands green; `basedpyright scripts/delegate/phase_table.py scripts/delegate/test_phase_table.py scripts/delegate/progress_history.py` reports `0 errors, 0 warnings`; `phase_table.py show --session-dir "${SESSION_DIR}" --zone America/Los_Angeles` on this run prints this plan's five phases with Phase 1 running.

### Phase 2 — The note is written to the vault at every report  · status: todo

#### Work Order

**Goal:** Every production unit's note appears in the vault and stays current: the recorder rewrites it at each phase start, progress report, phase finish and run finish, and a note left under an old name is removed.

**Spec:**

1. `phase_table.py refresh --session-dir <dir>`:
   - Read the plan's `> **Production: <name>** — unit `<unit>`; production doc `<path>`` line (format: `docs/delegate_plan_format.md`). No such line: exit 0 silently; a run outside a production writes nothing (author's scope call: the user's layout is `showrunners/<showrunner>/`).
   - Resolve the production doc against the plan's Git root; `read_production` gives `slug`, `showrunner_session`, `zone`.
   - Build the record and write it as JSON (the `--json` shape, plus `production`, `unit`, `zone`) to `<record root>/<slug>/<unit>.json`. Record root: `PHASE_TABLE_RECORDS`, default `_history_root()/phase-tables`. This file is the one record the dailies chart and the note both come from.
   - Vault root: `PHASE_TABLE_VAULT`, default `~/rust/hanadocs/showrunners`. When its parent (`~/rust/hanadocs`) does not exist, write the JSON only.
   - Note path: `<vault root>/<showrunner_session>/<file name>.md`. In this phase `file name` is the unit id.
   - Note content: frontmatter, then `# <file name>`, then `render(record, zone)`. Frontmatter keys, in this order: `phase_table: true`, `production`, `unit`, `showrunner`, `phase` (`"3 of 6"`), `percent`, `eta`, `plan_finish`, `updated` (ISO minutes in the User zone, or `null`).
   - Write through a temp file in the same directory and `os.replace`; create directories as needed; mode `0o644`.
   - Before the first write on a machine, when the vault is a Git checkout and its `.git/info/exclude` has no `showrunners/` line, append that line. Never edit the vault's `.gitignore` (the showrunner's call, 2026-10-08: it changes no shared file and needs no commit there).
   - Then remove every other `*.md` under `<vault root>/*/` whose frontmatter has `phase_table: true` and this `production` and `unit`, and remove a showrunner directory left empty. Never touch a file without `phase_table: true`.
2. `phase_table.py show` gains `--production <slug> --unit <id>`: print that unit's JSON record, exit 1 when there is none.
3. Recorder hook: `_refresh_phase_table(session_dir)` in `progress_history.py`, called as the last act of `_start_phase`, `_progress` (every path that printed a report), `_finish_phase` and `_finish_run`. It returns at once unless the state's plan file contains `> **Production:`. Otherwise it runs `[sys.executable, <phase_table.py>, "refresh", "--session-dir", …]` with a 10 s timeout, stdout discarded. On non-zero exit, timeout or `OSError` it prints one line to stderr, `phase table not written: <reason>`, and the recorder's own stdout and exit status are unchanged.
4. `commands/unit/report.md`, step 5: one paragraph — the `progress` call also rewrites this unit's phase note and record; nothing is written by hand; when the user asks for the phase table, paste `phase_table.py show --session-dir "${SESSION_DIR}" --zone <User zone>`.
5. `docs/as-built/plan-delegate-progress-history.md`: add the hook to the description of the four commands.

**Files:**
- `scripts/delegate/phase_table.py` — `refresh`, stale-note removal, `show --production --unit`.
- `scripts/delegate/test_phase_table.py` — refresh cases.
- `scripts/delegate/progress_history.py` — `_refresh_phase_table` and its four call sites.
- `scripts/delegate/test_progress_history.py` — hook cases.
- `commands/unit/report.md` — the step 5 paragraph.
- `docs/as-built/plan-delegate-progress-history.md` — the hook.

**Seats:** 1 writer + 1 tester — the note's bytes and the removal rule are fixed by the Spec.
- `impl` — `scripts/delegate/phase_table.py`, `scripts/delegate/progress_history.py`, `commands/unit/report.md`, `docs/as-built/plan-delegate-progress-history.md`
- `test` — `scripts/delegate/test_phase_table.py`, `scripts/delegate/test_progress_history.py`: with `PHASE_TABLE_VAULT` and `PHASE_TABLE_RECORDS` in a temp dir — the note's exact bytes; a second refresh after the showrunner's name changes moves the note and removes the empty directory; a hand-written note in the same folder survives; a plan with no Production line writes nothing; a failing `refresh` leaves `progress` output and exit status byte-identical.

**Constraints from prior phases:** Phase 1 built `build`, `render`, `show`, and the public `plan_phases`, `eta_band_seconds`, `plan_runs`. The record's JSON shape is Phase 1's `--json`.

**Acceptance gate:** both Test commands green; basedpyright clean on the changed files; after one `/unit:report` in this run, `~/rust/hanadocs/showrunners/natedev/phase-tables-unit.md` exists with this plan's table and `phase_table.py show --production build-followups --unit phase-tables-unit` prints its record.

### Phase 3 — A stated ETA is recorded  · status: todo

#### Work Order

**Goal:** When a unit states an ETA with `/unit:eta` or `/unit:eta_breakdown`, the note and the record carry that time, its range and its basis until it passes.

**Spec:**

1. Recorder subcommand `progress_history.py eta --session-dir <dir> --time <YYYY-MM-DDTHH:MM> [--earliest <…> --latest <…>] --basis <text>`. Times are local to the process `TZ` (units run the recorder under the User zone, `<ProductionUnit/>` item 11). It requires an active phase, appends an `eta_stated` event (`eta_at`, `eta_earliest_at`, `eta_latest_at` as epochs, `basis`) through `_append_event`, calls `_refresh_phase_table`, and prints `ETA recorded: <HH:MM zone>`. A time in the past, `--earliest` after `--time`, or `--latest` before it is refused with exit 2. One of `--earliest` / `--latest` without the other is refused.
2. `build`: the current phase's ETA is the last `eta_stated` of this phase instance while its `eta_at` is later than now — `source: "stated"`, plus `stated_at` and `basis`. Otherwise the projection, `source: "projected"`. `eta.first` = the phase instance's first `eta_stated` time, else `null`.
3. `render`: the ETA row gains a second row under it, `| ETA from | stated 09:50: <basis> |` or `| ETA from | projected from 60% done |`.
4. `commands/unit/eta.md`: before sending the answer, run the `eta` command with the time, range and basis being sent; the answer line is unchanged. `commands/unit/eta_breakdown.md`: when the breakdown moves the ETA, run the same command with the bullets' last end time.
5. `docs/as-built/plan-delegate-progress-history.md`: the `eta` command and the `eta_stated` event.

**Files:**
- `scripts/delegate/progress_history.py` — the `eta` subcommand.
- `scripts/delegate/phase_table.py` — stated ETA in `build`, the `ETA from` row.
- `scripts/delegate/test_progress_history.py`, `scripts/delegate/test_phase_table.py` — cases.
- `commands/unit/eta.md`, `commands/unit/eta_breakdown.md` — the recorder call.
- `docs/as-built/plan-delegate-progress-history.md` — the command and event.

**Seats:** 1 writer + 1 tester.
- `impl` — `scripts/delegate/progress_history.py`, `scripts/delegate/phase_table.py`, `commands/unit/eta.md`, `commands/unit/eta_breakdown.md`, `docs/as-built/plan-delegate-progress-history.md`
- `test` — the two test files: each refusal; a stated ETA wins over a later progress report; a passed one yields to the projection; a new phase starts with none; `first` holds across a restatement.

**Constraints from prior phases:** Phase 2's `_refresh_phase_table` and the record JSON; Phase 1's `current.eta` object, which gains `stated_at`, `basis`, `first`.

**Acceptance gate:** both Test commands green; basedpyright clean on the changed files; in this run, `eta --time <a time 30 min ahead> --basis "check"` changes the note's ETA rows and the record's `current.eta.source` to `stated`.

### Phase 4 — The note is named for the unit's session as it is now  · status: todo

**Blocked by:** G2 — enh-showrunner-unit's lookup checkpoint (`scripts/production/unit_lookup.py`) merged into `build-followups`.

#### Work Order

**Goal:** A unit's note is `showrunners/<showrunner>/<its session name now>.md`; after a rename the next report writes the new name and removes the old note.

**Spec:**

1. In `refresh`, `file name` = `unit_lookup.marked_units(slug)[unit].claude.name` when that is a `LiveClaude` with a non-empty name; in every other case (no marked tmux session, Claude not running, records unreadable, `OSError`) the unit id. Import `unit_lookup` from `scripts/production` by path, as `live_units.py` does. Read it; change nothing in it.
2. A name is made safe for a file: `/` and NUL become `-`; a leading `.` is dropped.
3. Nothing stores the name. Phase 2's removal rule already deletes the note written under the previous name, since it matches on frontmatter `production` and `unit`.
4. Frontmatter gains `session: <name>` after `unit`; the record JSON does not.

**Files:**
- `scripts/delegate/phase_table.py` — the name lookup.
- `scripts/delegate/test_phase_table.py` — cases using the lookup's own test stand-in (`UNIT_LOOKUP_TMUX`, `NOTIFIER_SESSIONS_DIR`).

**Seats:** 1 writer + 1 tester.
- `impl` — `scripts/delegate/phase_table.py`
- `test` — `scripts/delegate/test_phase_table.py`: a live name is used; a rename between two refreshes leaves one note; each fallback case uses the unit id.

**Constraints from prior phases:** Phase 2's `refresh` and removal rule. Re-read `unit_lookup.py` as merged: this Work Order was written from its pre-checkpoint form (`marked_units(slug) -> dict[str, MarkedUnit]`, `MarkedUnit.claude: LiveClaude | ClaudeNotRunning | ClaudeUnknown`).

**Acceptance gate:** Test commands green; basedpyright clean; this unit's note is `showrunners/natedev/phase-tables.md` and `phase-tables-unit.md` is gone.

### Phase 5 — The dailies read each unit's phase, start and ETA from the record  · status: todo

**Blocked by:** G2 — as Phase 4; this phase edits `scripts/production/dailies_input.py`, which that checkpoint changes.

#### Work Order

**Goal:** The dailies chart and each unit's note show the same phase, start and ETA, and the showrunner no longer types them; a retired unit's note is removed.

**Spec:**

1. `dailies_input.py`, per unit: read the record with `phase_table.read_record(slug, unit) -> PhaseRecord | None` (new, the reader behind `show --production --unit`). When it has a running phase and the judgment gives no `phase`, fill `phase` as `Phase <N> of <M>: <title>` and `started` as `YYYY-MM-DDTHH:MM` in the zone. When it has an ETA, fill `eta.time`, `eta.earliest`, `eta.latest`, `eta.percent` and `eta.first` in the renderer's forms (`HH:MM`, `+1` for tomorrow), and take the ETA's age from `stated_at` (a projected ETA is as old as the record's `updated`); the pane's ETA lines are then not read for that unit. A judgment value for any of these fields wins and is kept. A unit with no record, or no running phase, keeps today's path unchanged.
2. `phase_table.py prune --production-doc <doc>`: remove every `phase_table: true` note under `<vault root>/<showrunner_session>/` whose `production`/`unit` pair is not a live row of the doc's Units table, and its JSON record. `dailies_input.py` runs it once per build; a failure is reported as `phase tables: failed — <reason>` and does not stop the build.
3. `commands/showrunner/dailies.md`: Gather step 3 and the Input section say which fields the record supplies and that the judgment file overrides them.

**Files:**
- `scripts/production/dailies_input.py` — the record read, the prune call.
- `scripts/production/test_dailies_input.py` — cases.
- `scripts/delegate/phase_table.py` — `read_record`, `prune`.
- `scripts/delegate/test_phase_table.py` — prune cases.
- `commands/showrunner/dailies.md` — the two passages.

**Seats:** 2 writers — the split is by directory.
- `impl` — `scripts/production/dailies_input.py`, `scripts/production/test_dailies_input.py`, `commands/showrunner/dailies.md`
- `test` — opens as impl: `scripts/delegate/phase_table.py`, `scripts/delegate/test_phase_table.py`

**Constraints from prior phases:** the record JSON (Phases 1–3) and `PHASE_TABLE_RECORDS` / `PHASE_TABLE_VAULT`. `dailies_input.py` and `test_dailies_input.py` are enh-showrunner-unit's files: name them in the checkpoint notice as `also touches`, and trial-merge that unit's tip first (`<ProductionUnit/>` item 9). Line refs in Key files predate its checkpoint; re-read the file as merged.

**Acceptance gate:** all three Test commands green; basedpyright clean on the changed files; a dailies built with no `phase`, `started` or `eta` for this unit in the judgment file renders this unit's row from the record.

## Source

2026-10-08 08:20 PDT

two things we often ask for

1. phase eta - to be used in the dailies gantt chart (and elsewhere)
2. an overview of upcoming phases -  as a markdown table showing phase number, brief description, ETA (or predicted start / finish)

i would like to have a mechanism to keep this information succinctly up to date so i could just pull it up in hanadocs and look at the updated markdown table  -

we could keep it in sync with our current showrunner structures - i.e.
showrunners/hana
showrunners/natedev
etc.

and then
showrunners/hana/startup.md
showrunners/hana/widget.md
....
showrunners/natedev/showrunner-fixer.md
etc.

and whenever they wake up to do a /unit:report - as part of the /unit:report they update their own markdown file - using their current session name (and deleting an old one if if it was renamed)

this way i can stop asking for these ad hoc - and we have a natural place for them to be maintained

it can show current phase separately - with metadata and then keep the markdown table showing start end times for all past phases and upcoming phases as well - maybe sorted descending so the oldest phases are at the bottom

scripts can aid in this process

so that agents don't have to re-read/re-write every markdown file and possibly drift on format
