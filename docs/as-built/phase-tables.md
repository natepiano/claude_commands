# Phase tables

## What it is

A phase table shows one unit's current phase with its ETA, and every plan phase with its start and finish, recorded or predicted. The user asked for a phase's ETA and an overview of upcoming phases often enough to want a standing place to read them, kept current by scripts so that no agent re-reads or rewrites a note and its format cannot drift (user, 2026-10-08). `scripts/delegate/phase_table.py` rebuilds the table from the events [the recorder](plan-delegate-progress-history.md) writes for a unit run. The recorder calls it at every report, and the result is one note per production unit under `showrunners/<showrunner>/<unit session>.md` in the vault. A unit that states an ETA records it as one event in the same stream. [The dailies builder](showrunner-automation.md) asks the same script for every live unit's record, so the chart and the note read the same events.

## How it works

### Key files and commands

| File | Role |
| --- | --- |
| `scripts/delegate/phase_table.py` | Builds a `PhaseRecord` from run events, renders it as Markdown or JSON, and writes, moves and archives the notes. It writes no event. |
| `scripts/delegate/progress_history.py` | The recorder: the `eta` command, the `eta_stated` event, the note hook, and the public readers the table uses. |
| `scripts/production/dailies_input.py` | Calls `prune` and `show --production-doc` once per build and fills each unit's start and ETA from its record. |
| `commands/unit/report.md`, `commands/unit/eta.md`, `commands/unit/eta_breakdown.md` | Where a unit reports progress and records a stated ETA. |
| `commands/showrunner/dailies.md` | The `eta` row of the judgment contract and the meaning of `phase tables: unavailable`. |

```text
phase_table.py show    --session-dir <dir> [--zone <IANA>] [--json]
phase_table.py show    --production-doc <doc> --json
phase_table.py refresh --session-dir <dir>
phase_table.py prune   --production-doc <doc>
phase_table.py archive --production-doc <doc>
progress_history.py eta --session-dir <dir> --time <YYYY-MM-DDTHH:MM> [--earliest <YYYY-MM-DDTHH:MM> --latest <YYYY-MM-DDTHH:MM>] --basis <text>
```

`show` takes exactly one of `--session-dir` and `--production-doc`. With `--session-dir` it prints Markdown, or JSON under `--json`, in `--zone`; the default zone is `TZ`, then the machine's. With `--production-doc` it always prints JSON in the production doc's **User zone**. `NoState`, `NoPlan`, a `Refusal` or an unknown zone prints one line on stderr and exits 1.

### The record

```python
build_plan(plan_path: Path) -> PhaseRecord      # raises NoPlan
build(session_dir: Path) -> PhaseRecord         # raises NoState, NoPlan
render(record: PhaseRecord, zone: ZoneInfo) -> str
show(session_dir: Path, zone: ZoneInfo, json_output: bool = False) -> str
show_production(production_doc: Path) -> str
refresh(session_dir: Path) -> None
prune(production_doc: Path) -> None
archive(production_doc: Path) -> list[Path]

PhaseRecord(plan: Path, updated: datetime,
            current: OpenPhase | NoOpenPhase,
            phases: list[DonePhase | OpenPhase | TodoPhase],
            plan_finish: FinishedAt | PredictedFinish | UnknownFinish)
OpenPhase(phase, title, started,
          progress: ReportedPhaseProgress | PhaseProgressNotReported,
          eta: StatedEta | ProjectedEta | EtaUnavailable,
          first_stated: FirstStatedEtaTarget | EtaNeverStated)
FirstStatedEtaTarget(time, repair_rounds)
DonePhase(phase, title, times: CompletedPhaseTiming | PhaseTimingNotRecorded)
TodoPhase(phase, title, times: PredictedPhaseTiming | PhaseTimingNotPredicted)
StatedEta(time, range: EtaRange | NoEtaRange, stated_at, basis)
ProjectedEta(time, earliest, latest, as_of)
```

Every state is a named `NamedTuple`. Times are aware `datetime`s, and `None` appears only in the JSON. `build_plan` is the one builder. `build` reads the session state file (`progress_history.STATE_FILENAME`) for the plan path only (`project_plan_doc`, else `plan_doc`) and returns `build_plan` of it. `NoState` is a state file that is missing, unreadable or names no plan; `NoPlan` is a plan file that cannot be read, and its message names the path. A plan with no recorded run, or with no open phase, is a normal record.

- **Plan rows.** `progress_history.plan_phases(plan_path) -> list[PlanPhase]` gives the plan's headings in document order (`PlanPhase`: `id`, `title`, `done`). A heading with no status marker counts as done.
- **Runs.** `progress_history.plan_runs(plan_path) -> list[Path]` gives the plan's run files, oldest first. A run belongs to the plan when the `working_dir` and `plan_doc` of its `run_started` event resolve to the plan's path.
- **Matching.** A recorded phase instance attaches to a plan row by its recorded title (casefold, backticks removed, whitespace collapsed) when exactly one row has that title, else by phase id, across every run of the plan.
- **Done row.** Its times come from the title family of its last completed instance, last by write order: the family's earliest start, that instance's finish, and the family's summed `phase_elapsed_seconds`. A todo row never shows recorded times.
- **Open phase.** The newest instance with a `phase_started` and no `phase_finished` in the plan's newest run, unless that run has a `run_finished` event. A `phase_finished` of any status closes its instance, and an unfinished instance in an older run is not open. The open row's `started` is the earliest start among the open instance and the earlier instances of that plan row with the same recorded title. An open phase that matches no plan row shows in the heading line and marks no row running.
- **ETA.** The instance's last `eta_stated` while its `eta_at` is later than the current time, even when a progress report came after it. Otherwise the projection from its last `progress_reported` event: the report's time plus `progress_history.eta_band_seconds(phase_percent, elapsed, spread)`, with `as_of` the report's time; none exists at 0% or 100%, or with no elapsed time. Otherwise `EtaUnavailable`. A new phase starts with no stated ETA. `FirstStatedEtaTarget(time, repair_rounds)` holds the `eta_at` of the instance's first valid `eta_stated` event and the count of repair rounds started strictly after that event's `timestamp_epoch`. A repair round is a `fix_pass` of 1 or more on the instance's `pass_started` events, starts at the earliest such event for that number, and counts once across seats; round 0 and rounds that started before the statement do not count. The state holds across a restatement and after the ETA passes.
- **Predictions.** The typical duration is the median `seconds` of the completed done rows, else the open phase's ETA minus its start. The gap is the median seconds from a `completed` finish to the next `phase_started` in the same run, 0 with no sample. The first todo row starts one gap after the open phase's ETA of either source; with no ETA, one gap after the later of the current time and the open start plus the typical duration; with no open phase, at the current time. Each later row starts one gap after the row before it finishes, and finishes one typical duration later. With no typical duration every todo row is `PhaseTimingNotPredicted`.
- **Plan finish.** `FinishedAt` the latest recorded finish when every row is done and one has a recorded finish; `PredictedFinish` the latest of the open ETA and the predicted finishes while a row is unfinished; otherwise `UnknownFinish`.

### Markdown and JSON

```text
**Phase 3 of 4 — Current delivery**

| | |
| --- | --- |
| Started | 10-08 09:12 |
| Done | 60% |
| ETA | 10-08 10:23 (10:13 to 10:38) |
| ETA from | projected from 60% done |
| Plan finish | 10-08 11:25, predicted |
| Updated | 10-08 10:00 PDT |

| Phase | What it delivers | Status | Start | Finish |
| --- | --- | --- | --- | --- |
| 4 | Later delivery | predicted | 10-08 10:30 | 10-08 11:25 |
| 3 | Current delivery | running, 60% | 10-08 09:12 | 10-08 10:23 |
| 2 | Finished delivery | done in 0:55 | 10-08 08:10 | 10-08 09:05 |
| 1 | Archived delivery | done | — | — |
```

Rows are in descending plan order. Times are `MM-DD HH:MM` in `zone`, the zone abbreviation appears once on `Updated`, and an unknown cell is `—`. With no open phase the heading is `**No phase running — <done> of <total> done**` and the `ETA` and `ETA from` rows are absent. A stated ETA shows `| ETA from | stated 09:50: <basis> |`, with the basis's whitespace collapsed and `|` escaped, and its `ETA` cell carries parentheses only when a range came with it.

`show --json` prints `_json_record(record, zone)`, with ISO-8601 offset times and `null` for a key with no value:

```text
{plan, updated, plan_finish, current, phases}
current      null, or {phase, of, title, started, percent, eta, first_stated_eta_target, repair_rounds_since_first_stated_eta}
current.eta  null, or {time, earliest, latest, source: "stated" | "projected", stated_at, basis, as_of}
phases[]     {phase, title, status: "done" | "running" | "todo", start, finish, seconds}
```

`show --production-doc` prints one object keyed by unit id, with an entry for every live Units row (`add_unit.live_unit_table`). Each entry is that unit's record or `{"unavailable": "<reason>"}`. `ProductionUnitPlan(unit, plan)` takes the path from the Plan cell (`cell_value(cell).split(" ", 1)[0]`), resolved against the Worktree cell when relative.

### The note

`refresh` reads the plan's ``> **Production: <name>** — unit `<unit>`; production doc `<path>` `` line (`PRODUCTION_PATTERN`; format in `docs/delegate_plan_format.md`), resolves the production doc against the plan's Git root, and takes `slug` and `zone` from `read_production` (`scripts/production/add_unit.py`). A plan with no such line, or a vault root whose parent directory does not exist, exits 0 and writes nothing.

- **Path.** `<vault root>/<showrunner session name>/<file name>.md`. The folder is `showrunners.current_name(slug)` (`scripts/production/showrunners.py`). While that answers empty, the note is rewritten in the folder of the unit's most recently modified owned note; with no owned note `refresh` refuses with `the showrunner of <slug> is not running`.
- **Name.** `unit_lookup.marked_units(slug)[unit].claude.name` when that is a `LiveClaude` with a name; in every other case the unit id. `/` and NUL become `-`, and leading `.` characters are dropped.
- **Content.** Frontmatter of exactly three keys in this order (`phase_table: true`, `production: <slug>`, `unit: <unit id>`), then `# <file name>`, then `render(record, zone)`. `NoteOwnership(production, unit)` is those keys; a later refresh recognises this unit's own notes by them.
- **Publication.** A temp file in the target's directory, mode `0o644`. A target that carries this unit's keys is replaced with `os.replace`. A first write is published with `os.link`, so a file that appears at the target meanwhile is refused with `phase note appeared before publication: <path>`. A target with other keys or none is refused with `phase note target is not owned by <slug>/<unit>: <path>` and left unchanged. Before the write, when the vault root sits inside a Git checkout, `refresh` appends the vault root's path inside that checkout plus `/` (`showrunners/` for the default vault) to the checkout's `info/exclude`, once.
- **Archive.** `<vault>/archive/<showrunner folder>/<slug>/<name>.md`, outside the excluded `showrunners/` folder, so the vault's auto-commit records it. An existing target is never replaced: the next archive uses `-2`, then `-3`, and so on. Its placement keeps archived notes out of every live-note scan.
- **Moves and `prune`.** After the write, every other `*.md` under `<vault root>/*/` with the same ownership is removed, with its folder when that leaves it empty; a renamed unit session or showrunner moves the note by this rule. `prune` archives every owned note under `<vault root>/*/` whose `production` is the doc's slug and whose `unit` is not a live Units row, removing each folder it leaves empty.

### Recorder side

- **`eta`.** Reads its times in the process `TZ`, requires an active phase, appends one `eta_stated` event through `_append_event`, runs the hook, and prints `ETA recorded: <HH:MM zone>`. Beside the fields every event carries, the event holds `eta_at`, `basis` (stripped) and, with a range, `eta_earliest_at` and `eta_latest_at`, all times as epochs. It exits 2 with one stderr line for a malformed time, a time in the past, a time inside the hour the local clock skips, `--earliest` after `--time`, `--latest` before it, one range end alone, a blank `--basis`, or no active phase.
- **Hook.** `_refresh_phase_table(session_dir)` is the last act of `_start_phase`, `_progress`, `_finish_phase` and `_finish_run`, and follows the append in `_eta`. It returns at once unless the state's plan file contains `> **Production:`. Otherwise it runs `phase_table.py refresh` beside it under `sys.executable`, with a 10 s timeout and stdout discarded. A non-zero exit, a timeout or an `OSError` prints one stderr line, `phase table not written: <reason>`.
- **Public readers.** `plan_phases`, `plan_runs`, `eta_band_seconds(percent, elapsed, spread) -> EtaBand | EtaProjectionUnavailable` (`EtaBand`: `remaining`, `earliest`, `latest`, in seconds), `percent_spread`, `now_epoch` and `resolve_plan_path`.
- **Command files.** `commands/unit/report.md` step 5 says the `progress` call rewrites the note and that a requested table is `phase_table.py show --session-dir "${SESSION_DIR}" --zone <User zone>`. `commands/unit/eta.md` records the ETA, range and basis before the answer is sent, as `TZ=<User zone> python3 ~/.claude/scripts/delegate/progress_history.py eta …`. `commands/unit/eta_breakdown.md` runs the same command, with the bullets' last end time and no range, when the breakdown moves the ETA.

### Dailies side

[The dailies builder](showrunner-automation.md) (its **ETA** and **Order** bullets) covers the whole build and the renderer. This is the part that reads the records.

```python
phase_tables(production_doc: Path, zone: ZoneInfo) -> PhaseTablesAvailable | PhaseTablesUnavailable
prune_phase_tables(production_doc: Path) -> PhaseTablesPruned | PhaseTablesUnavailable
unit_phase_state(value: object, where: str, zone: ZoneInfo) -> NumberedOpenPlanPhase | NoNumberedOpenPlanPhase

NumberedOpenPlanPhase(number: int, started: datetime,
    percent: ReportedPhasePercent | PhasePercentNotReported,
    eta: CurrentStatedPhaseEta | CurrentProjectedPhaseEta | CurrentPhaseEtaUnavailable,
    first_stated: FirstStatedEtaTarget | EtaNeverStated)
NoNumberedOpenPlanPhase(reason: NoOpenPhase | OpenPhaseOutsidePlan | NonNumericPhaseId | UnitPhaseRecordUnavailable)

record_clock(moment: datetime, now: datetime) -> str
record_eta_stated(target: datetime, source: datetime, rendered_time: str) -> datetime
record_eta_range(target, earliest, latest, source, rendered_time, now) -> RendererSafeRecordEtaRange | RecordEtaRangeOmitted
```

- **Order.** `run` calls `prune_phase_tables` between `read_production` and `units_from_doc`, its one write before its checks, then `phase_tables` once. Record times are converted to `Production.zone`.
- **Which units.** A unit is record-backed when its open phase id is all digits, that phase has a `running` row in `phases`, and its number equals the one in the judgment's phase text (`judgment_phase_number`, `^Phase (\d+) of \d+: `). The record fills an absent `started`, and the ETA unless the judgment's `eta` holds `time` or `none`. Every other unit keeps the status-capture path (`eta_state`, `eta_value`).
- **State.** `record_eta_state` returns `EtaFresh | EtaStale | EtaPassed | EtaNone` in this order: no ETA and no first target is `EtaNone`; no ETA with a first target is `EtaPassed`; held, unchanged and not already requested is `EtaFresh`; a target at or before the build time is `EtaPassed`; a source time (`stated_at` or `as_of`) over one hour old is `EtaStale`; else `EtaFresh`. It keeps the `eta_seen.json` record under `<unit>|<phase text>` with `text` as the target in `YYYY-MM-DDTHH:MM`. A record whose `text` has another form came from a screen capture and is adopted with its `first_seen` and `requested`.
- **Value.** `record_eta_value` writes `time` as `record_clock` gives it (`HH:MM`, with `+N` when the target falls N days after the build's date), `stated`, the range, `percent` (the reported percent or `null`), `first` and `fixes` when the record holds a first stated target, and `why`: a stated ETA's `basis` with whitespace collapsed, or `it is now projected from <N>% done`. The judgment's own `eta` fields overwrite these, and a stale ETA gets `detail: set HH:MM` when the judgment gave none.
- **Renderer fit.** `record_eta_stated` returns the anchor the renderer reads a bare clock against: the source minute, or the target's own minute when `parse_time(rendered_time, source)` would land elsewhere. `record_eta_range` writes `earliest` and `latest` only when the renderer's `parse_range_end` reads both back as the record's minutes; else `RecordEtaRangeOmitted` and neither end is written.
- **Failure.** A failed or unparsable `show`, or a failed `prune`, prints `phase tables: unavailable — <reason>` and the build continues from the status capture.

### Roots and overrides

- `PHASE_TABLE_VAULT`: the vault root (`_vault_root`), default `~/rust/hanadocs/showrunners`.
- `PLAN_DELEGATE_HISTORY_DIR`: the recorder's history root (`_history_root`), default `~/.local/state/plan-delegate`; run files sit in its `runs/`. `PLAN_DELEGATE_NOW_EPOCH` replaces the clock (`now_epoch`).
- `NOTIFIER_STATE_DIR`, `NOTIFIER_SESSIONS_DIR`: where `scripts/production/showrunners.py` finds a showrunner, defaults `~/.local/state/notifier` and `~/.claude/sessions`.
- `DAILIES_PHASE_TABLE`: the table script the dailies builder calls (`phase_table_path`), default `scripts/delegate/phase_table.py`.
- `TZ`: the zone of `eta`'s typed times, and `show`'s default zone.

## Invariants

- The table writer never costs a report. No failure in it changes the recorder's stdout or exit status; it adds one stderr line.
- Two things are stored: the note, and a stated ETA as one `eta_stated` event. Everything a script can find again is rebuilt from the events on each request. No record file, record directory or cache exists.
- No session name is stored. A unit is keyed by production slug and unit id; the unit's and the showrunner's names are a file's and a folder's name, looked up again at each write (user, 2026-10-08).
- Scripts own the note's format. No command file tells an agent to write or edit the note.
- The events are the authority. The session state file supplies the plan path and nothing else; a phase in state with no `phase_started` event does not exist for the table.
- Open means started and not finished in the events. It is never proof that the unit's session is alive, and each caller decides liveness: the dailies keep `StatusBlock.state` for it.
- `refresh` never calls the recorder's CLI and never takes its session lock, which the recorder holds for the whole command the hook runs in.
- A note is moved, replaced or removed only when its frontmatter carries `phase_table: true`, a `production` and a `unit`. A hand-written file is never changed.
- `_json_record` is the only JSON form, and nothing stores it. The first stated target has one place in it: `current.first_stated_eta_target`.
- A state is a named type, never an optional field or a placeholder `datetime`.
- Every time in a note and in the production JSON is in the production doc's **User zone**.
- In the dailies a judgment value always wins, and a judgment `time` or `none` decides the ETA entirely.
- A phase-table failure in the dailies reads `unavailable`, never `failed`: `dailies.md` stops the report on a `<step>: failed` line.
- The dailies builder fits the renderer. `scripts/production/dailies_render.py` is not changed to suit a record.
- Tests never write to the real vault or state directory: every root has an override, and tests set it.

## Calibration and gotchas

- The hook's limit is 10 s and each Git call in `refresh` has 5 s. The dailies' stale threshold is one hour, for a stated and a projected ETA alike.
- The reader takes a shared lock on each run file and the recorder appends under an exclusive lock on the same file, so a read started while the recorder holds its lock blocks. `plan_runs` reads each run's first line with no lock.
- `_start_phase` writes session state before it appends `phase_started`. `start-phase` for the phase already active and `finish-phase` with no active phase return before the hook.
- Recorded history reuses phase numbers and rewords headings. Of 219 recorded phase starts, 177 match a plan heading by id and title, 29 by id only, 1 by title only and 12 not at all (ad hoc and follow-up ids). `_plan_finish` therefore looks at every row, not the last one.
- A stated ETA that passes leaves `current.eta` as the projection or `null`. The JSON never carries a passed stated time, and the passing writes no event: it is computed at read time. A kept ETA past its time is therefore a projection and always carries a range.
- A unit that stated an ETA, ran past it and reported no percent has `current.eta` `null` with a first target present. The dailies read that as `EtaPassed`, which asks for a new ETA once; with no first target it is `EtaNone`.
- A stated ETA with no progress report has `current.percent` `null` and stays record-backed. `basis` is never blank and may hold `|` or line breaks. A predicted row's JSON `status` is `todo`; only the Markdown says `predicted`.
- `show --production-doc` reads neither `--zone` nor `--json`. It turns only an unreadable plan into `unavailable`; an unreadable production doc fails the whole call.
- The machine's zone is Eastern and the User zone is Pacific: an `eta` call with no `TZ` reads a typed time three hours off. `eta.md` and `eta_breakdown.md` call the recorder installed under `~/.claude`, so a recorder change reaches them only once it is on main.
- `scripts/production/showrunners.py` reads its two directories once, at import. A test that runs `refresh` in a subprocess sets them in the child's environment; a test that calls `phase_table.main()` in process patches the module constants. `scripts/production/fake_showrunner.py` (`write_timer`, `write_session`) supplies the running showrunner a note needs.
- `add_unit.py` imports its siblings by bare name, so `phase_table.py` puts `scripts/production` on `sys.path` before its `scripts.production` imports. The form type-checks with no ignore and runs from any working directory.
- A vault on a file system without hard links refuses a note's first write with one line. A showrunner name of `.` or `..`, or one holding `/` or NUL, is refused.
- `production_lifecycle.py wrap` archives every generated note owned by the production before marking its doc wrapped, so another production can reuse the session name and publish a fresh note.
- The renderer reads a bare range end on the ETA's own day and moves it at most one day. An end more than a day from its ETA cannot be written, so both ends are left out.
- `phase tables: unavailable` after a failed prune does not mean the records went unread. A live builder run archives retired notes in the real vault unless `PHASE_TABLE_VAULT` points elsewhere.
- The recorder's test suite takes two to four minutes (195 s measured); the table suite about six seconds.

## Why

- **Rebuilt, not stored.** "Only the least necessary things written down to files - anything that is quickly discoverable by a script should be done that way" (user, 2026-10-08). A JSON record per unit, a stored session or showrunner name and a cache of the lookup all fail that test. The production doc holds no session name, so `current_name` reads the showrunner's from its update timer.
- **Scripts write the note.** Agents that re-read and re-write each note drift on format; a script at every report keeps one format and costs the agent nothing (user, 2026-10-08).
- **Named for the session, current phase apart, newest phase first.** The user reads `showrunners/<showrunner>/<unit>.md` beside the showrunner structure already in the vault, asked for the current phase shown separately with its metadata, and asked for the oldest phases at the bottom. A rename has to move the note, and the ownership keys make that possible with no stored name.
- **Title, then id.** History reuses phase numbers across runs of a plan; grouping events by id alone joins unrelated work.
- **A stated ETA is its own state.** It exists with no progress report and carries a basis, so it is not optional fields on `ProjectedEta`, and `ReportedPhaseProgress` keeps `percent` only.
- **The first target sits outside `eta`.** `current.eta` alone cannot tell a passed promise from a phase that never stated one. One key carries it, present whether or not `eta` is `null`.
- **`os.link` for a first write.** Two names that become one path cannot take each other's note.
- **`info/exclude`, not `.gitignore`.** The vault's `.gitignore` is a shared file; `info/exclude` needs no commit.
- **A projection writes its own `why`.** The renderer refuses an ETA move of 15 minutes or more with no reason. Refusing the build instead would bring back hand-typed reasons.
- **The builder checks against the renderer's own parser.** The range and stated-moment rules live in the renderer, so the builder writes only what `parse_range_end` reads back. `RecordEtaRangeOmitted` stays reachable for an end more than a day from its ETA.
- **Older `eta_seen.json` text is adopted, not parsed.** Adoption keeps `first_seen` and `requested`, so a held unit is not asked twice.
