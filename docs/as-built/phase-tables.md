# Phase tables

## What it is

A phase table shows one unit's current phase with its ETA, and every plan phase with its start and finish, recorded or predicted. The user asked for a phase's ETA and an overview of upcoming phases often enough to want a standing place to read them, kept current by scripts so that no agent re-reads or rewrites a note and its format cannot drift (user, 2026-10-08). `scripts/direct/phase_table.py` rebuilds the table from the events [the recorder](unit-progress-history.md) writes for a unit run. The recorder calls it at every report, and the result is one note per production unit under `showrunners/<showrunner>/<unit session>.md` in the vault. A unit that states an ETA records it as one event in the same stream. [The dailies builder](showrunner-automation.md) asks the same script for every live unit's record, so the chart and the note read the same events.

## How it works

### Key files and commands

| File | Role |
| --- | --- |
| `scripts/direct/phase_table.py` | Builds a `PhaseRecord` from run events, renders it as Markdown or JSON, and writes, moves and archives the notes. It writes no event. |
| `scripts/direct/progress_history.py` | The recorder: the `eta` command, the `eta_stated` event and the note hook. |
| `scripts/direct/plan_history.py` | The plan and run readers the table and the recorder share. `phase_table` imports it, never `progress_history`; `progress_history` imports `phase_table` only inside function bodies (`_project_chain_eta`, `_phase_eta_line`). |
| `scripts/production/dailies_input.py` | Calls `show --production-doc` once per build and fills each unit's start and ETA from its record. |
| `scripts/production/prune_phase_notes.py` | Runs `prune` in-process for every update timer's production; the agent-notes job runs it on the interval in `config/agent_notes.toml`. |
| `commands/unit/report.md`, `commands/unit/eta.md`, `commands/unit/eta_breakdown.md` | Where a unit reports progress and records a stated ETA. |
| `commands/showrunner/dailies.md` | The `eta` row of the judgment contract and the meaning of `phase tables: unavailable`. |

```text
phase_table.py show    --session-dir <dir> [--zone <IANA>] [--json]
phase_table.py show    --production-doc <doc> --json
phase_table.py refresh --session-dir <dir>
phase_table.py prune   --production-doc <doc>
phase_table.py archive --production-doc <doc>
progress_history.py eta --session-dir <dir> --time <t> [--phase-id <id>] [--earliest <t> --latest <t>] --basis stated|checkpoint|eta-command
```

`show` takes exactly one of `--session-dir` and `--production-doc`. With `--session-dir` it prints Markdown, or JSON under `--json`, in `--zone`; the default zone is the user's zone setting (`scripts/lib/user_zone.py`). With `--production-doc` it always prints JSON in the user's zone setting. `NoState`, `NoPlan`, a `Refusal` or an unknown zone prints one line on stderr and exits 1.

### The record

`build` uses the owner's staff folder when no working directory is supplied.
A failed staff read remains an error in the result.
The reader never substitutes the caller's current directory.
Phase notes store the unit's fixed staff id in `unit:`.

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
            plan_finish: FinishedAt | PredictedFinish | UnknownFinish,
            parallel: CouldStart | NothingToStart | DependenciesUnknown)
PhaseHistory(first_eta: datetime | EtaNeverStated, moves: int,
             eta_trail: list[tuple[datetime, datetime]],
             finished_at: datetime | PhaseNotFinished,
             delta_minutes: float | FinishDeltaUnavailable,
             fix_done: int, fix_open_passes: int, findings: dict[str, int])
             # properties: fix_running (0 or 1), fix_rounds (fix_done + fix_open_passes)
OpenPhase(PhaseHistory)(phase, title, started,
          progress: ReportedPhaseProgress | PhaseProgressNotReported,
          eta: StatedEta | ProjectedEta | EtaUnavailable,
          first_stated: FirstStatedEtaTarget | EtaNeverStated,
          instance_id: str,
          schedule_end: datetime | EtaUnavailable)
FirstStatedEtaTarget(time, repair_rounds)
DonePhase(PhaseHistory)(phase, title, times: CompletedPhaseTiming | PhaseTimingNotRecorded)
TodoPhase(PhaseHistory)(phase, title, times: PredictedPhaseTiming | PhaseTimingNotPredicted,
          gate: NoGate | TimedGate | UntimedGate)
StatedEta(time, range: EtaRange | NoEtaRange, stated_at, basis)
ProjectedEta(time, earliest, latest, as_of)
TimedGate(gate, until, waits_on)
UntimedGate(gate, waits_on)
FinishedAt(at)
PredictedFinish(at)
UnknownFinish()
```

Every state is a named `NamedTuple`, except the row types, which are frozen dataclasses on the `PhaseHistory` base. Times are aware `datetime`s, and `None` appears only in the JSON. `build_plan` is the one builder. `build` reads the session state file (`plan_history.STATE_FILENAME`) for the plan path only (`project_plan_doc`, else `plan_doc`) and returns `build_plan` of it. `NoState` is a state file that is missing, unreadable or names no plan; `NoPlan` is a plan file that cannot be read, and its message names the path. A plan with no recorded run, or with no open phase, is a normal record.

- **Plan rows.** `plan_history.plan_phases(plan_path) -> list[PlanPhase]` gives the plan's headings in document order (`PlanPhase`: `id`, `title`, `done`, `depends_on`, `blocked_by`). A heading with no status marker counts as done. `depends_on` is the phase ids on the first `**Depends on:**` line before the next heading (`none` gives `[]`, no line gives `None`); `blocked_by` is the `G<k>` of a `**Blocked by:**` line, else `""`.
- **Runs.** `plan_history.plan_runs(plan_path) -> list[Path]` gives the plan's run files, oldest first. A run belongs to the plan when the `working_dir` and `plan_doc` of its `run_started` event resolve to the plan's path.
- **Matching.** A recorded phase instance attaches to a plan row by its recorded title (casefold, backticks removed, whitespace collapsed) when exactly one row has that title, else by phase id, across every run of the plan.
- **Done row.** Its times come from the title family of its last completed instance, last by write order: the family's earliest start, that instance's finish, and the family's summed `phase_elapsed_seconds`. A todo row never shows recorded times.
- **Open phase.** The newest instance with a `phase_started` and no `phase_finished` in the plan's newest run, unless that run has a `run_finished` event. A `phase_finished` of any status closes its instance, and an unfinished instance in an older run is not open. The open row's `started` is the earliest start among the open instance and the earlier instances of that plan row with the same recorded title. An open phase that matches no plan row shows in the heading line and marks no row running.
- **Stated ETAs of an instance.** `_instance_eta_events(runs, instance)` selects the `eta_stated` events whose `eta_phase_id` (falling back to `phase_id` for events without one) is the instance's phase id, in the same run, after the previous start or finish of that phase id and before its next start. An ETA recorded before its phase starts, such as the checkpoint notice's next-phase ETA, counts for that phase. `_latest_stated_eta` builds on it.
- **ETA.** The instance's latest stated ETA, even when a progress report came after it. A passed one is advanced to now for scheduling, its range shifted by the same delay, so a phase never ends in the past. With none stated, the projection from its last `progress_reported` event: the report's time plus `plan_history.eta_band_seconds(phase_percent, elapsed, spread)`, with `as_of` the report's time; none exists at 0% or 100%, or with no elapsed time. Otherwise `EtaUnavailable`. `OpenPhase.instance_id` names the instance selected as open. `FirstStatedEtaTarget(time, repair_rounds)` holds the `eta_at` of the instance's first valid `eta_stated` event and the count of repair rounds started strictly after that event's `timestamp_epoch`. A repair round is a `fix_pass` of 1 or more on the instance's `pass_started` events, starts at the earliest such event for that number, and counts once across workers; round 0 and rounds that started before the statement do not count. The state holds across a restatement and after the ETA passes.
- **History.** Each row carries its `PhaseHistory`, read from its own title family only, so a retired task's attempts under a reused phase number are excluded. `eta_trail` is every `[(eta_at, stated_at)]` promise from `_instance_eta_events`. A row with no instance takes each `eta_stated` event whose `eta_phase_id` names it, except an event that `_instance_eta_events` gives to an earlier instance with that number, or one stated before that instance's recorded finish. An earlier instance with no finish excludes only its own events. `first_eta` is its first entry; `moves` counts only time changes, since unchanged restatements are recorded too. `finished_at` and `delta_minutes` (finish minus the last promise) are set on a completed done row. `fix_done` and `fix_open_passes` count finished and open `fix` passes; `findings` counts `finding_opened` events by severity (`blocker`, `minor`, `nit`).
- **Predictions.** The typical duration is the median `seconds` of the completed done rows, else the open phase's ETA minus its start. The gap is the median seconds from a `completed` finish to the next `phase_started` in the same run, 0 with no sample. The open phase's end, `OpenPhase.schedule_end`, is its ETA of either source; with no ETA, the later of the current time and the open start plus the typical duration. The first todo row starts one gap after that end; with no open phase, at the current time. Each later row starts one gap after the row before it finishes, and finishes one typical duration later. With no typical duration every todo row is `PhaseTimingNotPredicted`.
- **Gates.** Inside a production, a todo row whose plan heading carries `**Blocked by:** G<k>` takes its gate from the production doc's `## Gates` table (`Gate | Waiting | Waits on | Clears when`, read by `_production_gates`). `Clears when` starting `cleared` gives `NoGate`; a `YYYY-MM-DD HH:MM` (first match, user zone) gives `TimedGate(gate, until, waits_on)`, which holds that row's start to `until`; no time, or a gate missing from the table, gives `UntimedGate(gate, waits_on)`, which leaves that row and every later one `PhaseTimingNotPredicted` and carries the gate onto them. Outside a production `Blocked by` is ignored. The Markdown status of a gated row reads `gated until <ddd DD Mon HH:MM>` or `gated by G<k>: <Waits on>`.
- **Parallel.** `PhaseRecord.parallel` is `CouldStart(phases) | NothingToStart(reason) | DependenciesUnknown(phases)`. A row is running when its id was started and not finished in the newest unfinished run (any open phase, not only the newest), and waiting when it is neither done nor running. Any waiting row with `depends_on` `None` gives `DependenciesUnknown`. Otherwise a waiting row whose dependencies are all done and that has no gate could start; with none, `NothingToStart` names the first waiting row's unmet phases with their state (`running`, `todo`, `not in the plan`) and its gate, or `no phase is waiting`. `parallel_line` prints it as `Parallel: Phase 5, Phase 6 could start`, `Parallel: none — <reason>` or `Parallel: unknown — …`; `phase_table.py parallel --session-dir <dir>` prints that line, and JSON adds `parallel: {could_start, line}`.
- **Plan finish.** A completed plan gives `FinishedAt` at its latest recorded finish.
  An unfinished plan gives `PredictedFinish` from the latest open ETA, predicted finish, or latest promise of an open or not-started phase.
  No usable finish gives `UnknownFinish`.

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

Rows are in descending plan order. Times are `MM-DD HH:MM` in `zone`, the zone abbreviation appears once on `Updated`, and an unknown cell is `—`. `_heading_text(record)` gives the heading's words (`Phase <id> of <total> — <title>` for an open phase that is one of the plan's rows; `Phase <id> — <title> · not in this plan` when the open phase was recorded against this plan but is not one of its phases, because its run names a different plan than the one the unit is working) and `render` wraps them in bold. With no open phase the heading is `**No phase running — <done> of <total> done**` and the `ETA` and `ETA from` rows are absent. A stated ETA shows `| ETA from | stated 09:50: <basis> |`, with the basis's whitespace collapsed and `|` escaped, and its `ETA` cell carries parentheses only when a range came with it.

`show --json` prints `_json_record(record, zone)`, with ISO-8601 offset times and `null` for a key with no value:

```text
{plan, updated, plan_finish, current, phases}
current      null, or {phase, of, title, started, percent, eta, first_stated_eta_target, repair_rounds_since_first_stated_eta}
current.eta  null, or {time, earliest, latest, source: "stated" | "projected", stated_at, basis, as_of}
phases[]     {phase, title, status: "done" | "running" | "todo", start, finish, seconds,
              first_eta, moves, eta_trail: [[target, stated_at]], finished_at, delta_minutes, fix_rounds, findings}
```

JSON writes the finish time for `FinishedAt` and `PredictedFinish`, and `null` for `UnknownFinish`.
Markdown writes the recorded time, the predicted time with `, predicted`, or `—`, respectively.
The Units table reads `build_plan` records for progress and ETA facts.

`current.repair_rounds_since_first_stated_eta` is the `FirstStatedEtaTarget`'s count when a first stated target exists, else `null`, and is present even when `current.eta` is `null`. The Markdown shows no count.

`show --production-doc` prints one object keyed by unit id, with an entry for every active unit staff row under the document's owner. Each entry is that unit's record or `{"unavailable": "<reason>"}`. `ProductionUnitPlan(unit, plan)` takes the `.unit.toml` plan path, resolved against the staff worktree.

### The note

`refresh` finds the unit staff row through its plan.
The parent showrunner row supplies the production name and `doc.path`.
A plan outside a production writes no note.
A run whose plan file was removed, as for ad hoc work after the as-built, writes no note and prints nothing.

- **Path.** `<vault root>/<showrunner session name>/<file name>.md`. The folder is `showrunners.current_name(name)` (`scripts/production/showrunners.py`). While that answers empty, the note is rewritten in the folder of the unit's most recently modified owned note; with no owned note `refresh` refuses with `the showrunner of <showrunner name> is not running`.
- **Name.** `unit_lookup.labeled_sessions(name)[unit].claude.name` when that is a `LiveClaude` with a name; in every other case the unit id. `/` and NUL become `-`, and leading `.` characters are dropped.
- **Content.** The frontmatter keeps `phase_table: true`, `showrunner`, `production_file`, `unit`, and `now`. `production_file` and `now` use JSON quoting. The body keeps the unit heading and phase rows with ids, titles, and status. Rows appear in reverse plan order. The note has no timestamps, durations, progress percentages, or ETAs. `show` and the JSON output keep those values. The production link points to the promoted document when available. The link uses a vault wiki link, a GitHub URL, or a local file URI. Ownership comes from the staff rows.
- **Publication.** The writer checks ownership under the folder lock. Identical content keeps the existing bytes, inode, and modification time. Changed content uses a temporary file with mode `0o644`. The writer replaces an owned target with `os.replace`. A first write uses `os.link` and refuses a target that appears during publication. A foreign target remains unchanged. The writer does not add a Git exclusion. Existing installations must remove the old `showrunners/` entry from the vault’s `info/exclude`. The existing `agent-notes` vault part includes live phase notes only when its own generated notes already need a content commit. Selection uses the writer’s `phase_note_ownership` parser. Deleted notes use their content from `HEAD` for this check. It includes only owned live-note deletions. It leaves unrelated changes alone. Phase notes alone cause no commit. The existing ten-minute `hanadocs-push` job pushes these commits. No new job or schedule is required.
- **Archive.** `_archive_note(candidate, vault_root, name)` moves an owned note to `<vault>/archive/<showrunner folder>/<showrunner name>/<name>.md`. Here, `<vault>` is the parent of the live-note root. Live-note scans and commits do not include archive files. The move uses `os.link` then `unlink`. An existing target remains unchanged. A repeated archive uses `<stem>-2.md`, then `<stem>-3.md`. An operating system error raises `Refusal` with the path and cause. A failed unlink removes the new link first. The writer removes an empty source folder after the move.
- **`archive`.** Archives every owned note whose `production` is the showrunner staff name, live or retired unit alike; hand-written notes and other productions' notes stay. It prints one archived path per line and exits 0 also when nothing moved; a vault root whose parent does not exist archives nothing. `production_lifecycle.py wrap` runs it.
- **Moves and pruning.** After a write, the writer removes other notes with the same ownership.
  It removes a folder when that removal leaves it empty.
  `prune` resolves ownership through staff and archives notes whose unit is no longer live in that production.
  Fixed unit ids preserve ownership through a rename.

### Recorder side

- **`eta`.** Reads its times in the user's zone setting, `HH:MM` as today or `YYYY-MM-DDTHH:MM`, appends one `eta_stated` event through `_append_event`, runs the hook, and prints `ETA recorded: <HH:MM zone>`. Beside the fields every event carries, the event holds `eta_at`, `basis` (stripped), `eta_phase_id` (`--phase-id`, else the active phase's id) and, with a range, `eta_earliest_at` and `eta_latest_at`, all times as epochs. `basis` is `checkpoint`, `eta-command` or `stated`. It exits 2 with one stderr line for a malformed time, a time in the past, a time inside the hour the local clock skips, `--earliest` after `--time`, `--latest` before it, one range end alone, a blank `--basis`, or no active phase and no `--phase-id`.
- **Hook.** `_refresh_phase_table(session_dir)` follows phase, progress, finish and ETA records.
  It skips a plan with no staff unit. Otherwise it runs `phase_table.py refresh`.
  A failure prints `phase table not written: <reason>` and keeps the recorder's result.
- **Report.** `progress` reads `build_plan`: its short report's ETA line (`_phase_eta_line`, the reported phase's own row and gate, its latest promise rather than `schedule_end`) and the clocks table's Project ETA (`_project_chain_eta`, the plan finish, `after G<k>` under an untimed gate). `scripts/production/unit_phases.py` builds the Units table from the same record.
- **Shared readers.** `plan_history` holds `plan_phases`, `plan_runs`, `eta_band_seconds(percent, elapsed, spread) -> EtaBand | EtaProjectionUnavailable` (`EtaBand`: `remaining`, `earliest`, `latest`, in seconds), `percent_spread`, `now_epoch` and `resolve_plan_path`, with `history_root`, `BLOCKED_BY_PATTERN`, `STATE_FILENAME` and `PlanPhase`. Tests patch `plan_history.now_epoch` for the clock.
- **Command files.** `commands/unit/report.md` step 5 says the `progress` call rewrites the note and that a requested table is `phase_table.py show --session-dir "${SESSION_DIR}"`. `<ProgressContract>` in `commands/unit/direct.md` records every ETA the unit director states before stating it (`--phase-id` for a phase other than the active one) and states the recorder's printed time. `docs/direct/phase_end.md` step 2 and `docs/production_format.md` item 3 record the next phase's ETA with `--phase-id <next> --basis checkpoint` before the checkpoint notice. `commands/unit/eta.md` records the ETA, range and `--basis eta-command` before the answer is sent, as `~/.claude/scripts/lib/py ~/.claude/scripts/direct/progress_history.py eta …`. `commands/unit/eta_breakdown.md` runs the same command on every answer, with the bullets' last end time and no range.

### Dailies side

[The dailies builder](showrunner-automation.md) (its **ETA** and **Order** bullets) covers the whole build and the renderer. This is the part that reads the records.

```python
phase_tables(production_doc: Path, zone: ZoneInfo) -> PhaseTablesAvailable | PhaseTablesUnavailable
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

- **Order.** `run` calls `phase_tables` once, after `units_from_doc`. It writes nothing to the vault. Record times are converted to `Production.zone`.
- **Which units.** A unit is record-backed when its open phase id is all digits, that phase has a `running` row in `phases`, and its number equals the one in the judgment's phase text (`judgment_phase_number`, `^Phase (\d+) of \d+: `). The record fills an absent `started`, and the ETA unless the judgment's `eta` holds `time` or `none`. Every other unit keeps the status-capture path (`eta_state`, `eta_value`).
- **State.** `record_eta_state` returns `EtaFresh | EtaStale | EtaPassed | EtaNone` in this order: no ETA and no first target is `EtaNone`; no ETA with a first target is `EtaPassed`; held, unchanged and not already requested is `EtaFresh`; a target at or before the build time is `EtaPassed`; a source time (`stated_at` or `as_of`) over one hour old is `EtaStale`; else `EtaFresh`. It keeps the `eta_seen.json` record under `<unit>|<phase text>` with `text` as the target in `YYYY-MM-DDTHH:MM`. A record whose `text` has another form came from a screen capture and is adopted with its `first_seen` and `requested`.
- **Value.** `record_eta_value` writes `time` as `record_clock` gives it (`HH:MM`, with `+N` when the target falls N days after the build's date), `stated`, the range, `percent` (the reported percent or `null`), `first` and `fixes` when the record holds a first stated target, and `why`: a stated ETA's `basis` with whitespace collapsed, or `it is now projected from <N>% done`. The judgment's own `eta` fields overwrite these, and a stale ETA gets `detail: set HH:MM` when the judgment gave none.
- **Renderer fit.** `record_eta_stated` returns the anchor the renderer reads a bare clock against: the source minute, or the target's own minute when `parse_time(rendered_time, source)` would land elsewhere. `record_eta_range` writes `earliest` and `latest` only when the renderer's `parse_range_end` reads both back as the record's minutes; else `RecordEtaRangeOmitted` and neither end is written.
- **Repair rounds.** The builder's `FirstStatedEtaTarget(time, repair_rounds)` mirrors the record's. `unit_phase_state` reads `repair_rounds_since_first_stated_eta` only when the first target is not `null`, and requires an `int` (not a `bool`) of 0 or more; anything else raises `ValueError("<where>.current.repair_rounds_since_first_stated_eta: expected a whole number from 0")`. `EtaPassed`, `EtaNone` and an unavailable ETA carry no `fixes`.
- **Parallel check.** For each kept unit, `run` prints `parallel check: <unit>: Phase <N> could start` per id in the record's `parallel.could_start`; a record without `parallel` prints nothing.
- **Failure.** A failed or unparsable `show`, or a record that fails decoding, prints `phase tables: unavailable — <reason>` and the build continues from the status capture.

### Roots and overrides

- `PHASE_TABLE_VAULT`: the vault root (`_vault_root`), default `~/rust/hanadocs/showrunners`.
- `UNIT_HISTORY_DIR`: the recorder's history root (`_history_root`), default `~/.local/state/unit-history` (`~/.local/state/plan-delegate` until `unit_runs.py move-history` moves it); run files sit in its `runs/`. `UNIT_NOW_EPOCH` replaces the clock (`now_epoch`).
- `NOTIFIER_STATE_DIR`, `NOTIFIER_SESSIONS_DIR`: where `scripts/production/showrunners.py` finds a showrunner, defaults `~/.local/state/notifier` and `~/.claude/sessions`.
- `DAILIES_PHASE_TABLE`: the table script the dailies builder calls (`phase_table_path`), default `scripts/direct/phase_table.py`.

## Invariants

- The table writer never costs a report. No failure in it changes the recorder's stdout or exit status; it adds one stderr line.
- Two things are stored: the note, and a stated ETA as one `eta_stated` event. Readers place an ETA by `eta_phase_id`, never `phase_id`, which names the phase active when it was recorded. Everything a script can find again is rebuilt from the events on each request. No record file, record directory or cache exists.
- No session name is stored. A unit has a fixed staff id; the unit's and the showrunner's names are a file's and a folder's name, looked up again at each write (user, 2026-10-08).
- Scripts own the note's format. No command file tells an agent to write or edit the note.
- The events are the authority. The session state file supplies the plan path and nothing else; a phase in state with no `phase_started` event does not exist for the table.
- Open means started and not finished in the events. It is never proof that the unit's session is alive, and each caller decides liveness: the dailies keep `StatusBlock.state` for it.
- `refresh` never calls the recorder's CLI and never takes its session lock, which the recorder holds for the whole command the hook runs in.
- A note is moved, replaced or removed only when its frontmatter carries `phase_table: true`, a `production_file` naming a production doc (or, on an older note, a `production`) and a `unit`. A hand-written file is never changed.
- `_json_record` is the only JSON form, and nothing stores it. The first stated target has one place in it: `current.first_stated_eta_target`.
- A state is a named type, never an optional field or a placeholder `datetime`.
- The production JSON uses the user’s zone setting. The tracked note has no times.
- In the dailies a judgment value always wins, and a judgment `time` or `none` decides the ETA entirely.
- A phase-table failure in the dailies reads `unavailable`, never `failed`: `dailies.md` stops the report on a `<step>: failed` line.
- The dailies builder fits the renderer. `scripts/production/dailies_render.py` is not changed to suit a record.
- Tests never write to the real vault or state directory: every root has an override, and tests set it.

## Calibration and gotchas

- The hook's limit is 10 s and each Git call in `refresh` has 5 s. The dailies' stale threshold is one hour, for a stated and a projected ETA alike.
- The reader takes a shared lock on each run file and the recorder appends under an exclusive lock on the same file, so a read started while the recorder holds its lock blocks. `plan_runs` reads each run's first line with no lock.
- `_start_phase` writes session state before it appends `phase_started`. `start-phase` for the phase already active and `finish-phase` with no active phase return before the hook.
- Recorded history reuses phase numbers and rewords headings. Of 219 recorded phase starts, 177 match a plan heading by id and title, 29 by id only, 1 by title only and 12 not at all (ad hoc and follow-up ids). `_plan_finish` therefore looks at every row, not the last one.
- A stated ETA that passes stays the stated ETA with its time advanced to now (`source` `stated`), and the passing writes no event: it is computed at read time. The dailies read a target at or before the build time as `EtaPassed`, which asks for a new ETA once. The promise itself stays in `eta_trail`; only scheduling uses the advanced time.
- A stated ETA with no progress report has `current.percent` `null` and stays record-backed. `basis` is never blank and may hold `|` or line breaks. A predicted row's JSON `status` is `todo`; only the Markdown says `predicted`.
- `show --production-doc` reads neither `--zone` nor `--json`. It turns only an unreadable plan into `unavailable`; an unreadable production doc fails the whole call.
- An `eta` call reads a typed time in the user's zone setting, never the machine's zone or the process `TZ` (2026-10-12). `HH:MM` means today only; a later-day ETA needs the full date. `eta.md` and `eta_breakdown.md` call the recorder installed under `~/.claude`, so a recorder change reaches them only once it is on main.
- `scripts/production/showrunners.py` reads its two directories once, at import. A test that runs `refresh` in a subprocess sets them in the child's environment; a test that calls `phase_table.main()` in process patches the module constants. `scripts/production/fake_showrunner.py` (`write_timer`, `write_session`) supplies the running showrunner a note needs.
- `phase_table.py` imports production helpers through their `scripts.production` package names.
  Entry scripts use the [shared package bootstrap](merge-gate-and-test-speed.md#python-package-imports).
- A vault on a file system without hard links refuses a note's first write with one line. A showrunner name of `.` or `..`, or one holding `/` or NUL, is refused.
- `production_lifecycle.py wrap` archives every generated note owned by the production before marking its doc wrapped, so another production can reuse the session name and publish a fresh note.
- The archive sits outside `showrunners/`. Live-note scans use `vault_root.glob("*/*.md")` and do not include archive files.
- `refresh` deletes, not archives, the older copy of a moved unit's note; the newer note carries the same phases.
- Repair-round starts go through `_moment` to datetimes, the representation of `stated_at`. A raw float epoch compared against the microsecond-rounded `stated_at` would count an equal-moment round as later.
- The renderer reads a bare range end on the ETA's own day and moves it at most one day. An end more than a day from its ETA cannot be written, so both ends are left out.
- **Pruning (2026-10-13; user: "Yes do the part C").** `prune_phase_notes.py` reads each `showrunner-*` folder under `NOTIFIER_STATE_DIR` and takes the production doc from the staff row that the folder's id names; no session record is read, so a stopped showrunner's production is pruned too. It calls `phase_table.prune(doc)` for each, in one process, and uses the standard library alone. A timer with no readable doc, or a prune that raises `Refusal`, `OSError` or `UnicodeError`, prints `phase notes not pruned for <showrunner name>: <reason>` to stderr and the next production still runs; the exit is 1 when any failed. Success prints nothing. `scripts/maintenance/agent_notes_job.py` runs it as its own process with the interval and limit in `config/agent_notes.toml`, so a failure never costs another maintenance part. Natedev owns this job; nothing prunes on the Mac (accepted 2026-10-13: no Mac production needed it). Before this, a dailies pruned, so a production that built none kept its retired units' notes.
- A live run of `prune_phase_notes.py` archives retired notes in the real vault unless `PHASE_TABLE_VAULT` points elsewhere.
- The recorder's tests (`scripts/direct/test_progress_history.py`) run `progress_history.main()` in-process through `recorder_fixture.py` and are still among the merge gate's slowest modules; the table suite takes about six seconds. See [merge-gate-and-test-speed.md](merge-gate-and-test-speed.md).

## Why

- **Rebuilt, not stored.** "Only the least necessary things written down to files - anything that is quickly discoverable by a script should be done that way" (user, 2026-10-08). A JSON record per unit, a stored session or showrunner name and a cache of the lookup all fail that test. The production doc holds no session name, so `current_name` reads the showrunner's from its update timer.
- **Scripts write the note.** Agents that re-read and re-write each note drift on format; a script at every report keeps one format and costs the agent nothing (user, 2026-10-08).
- **Named for the session, current phase apart, newest phase first.** The user reads `showrunners/<showrunner>/<unit>.md` beside the showrunner structure already in the vault, asked for the current phase shown separately with its metadata, and asked for the oldest phases at the bottom. A rename has to move the note, and the ownership keys make that possible with no stored name.
- **Title, then id.** History reuses phase numbers across runs of a plan; grouping events by id alone joins unrelated work.
- **A stated ETA is its own state.** It exists with no progress report and carries a basis, so it is not optional fields on `ProjectedEta`, and `ReportedPhaseProgress` keeps `percent` only.
- **The first target sits outside `eta`.** `current.eta` alone cannot tell a passed promise from a phase that never stated one. One key carries it, present whether or not `eta` is `null`.
- **`os.link` for a first write.** Two names that become one path cannot take each other's note.
- **Stable tracked notes.** Phase changes update notes. Time, ETA, and progress changes do not update notes. Existing content commits carry note changes to the Mac.
- **A projection writes its own `why`.** The renderer refuses an ETA move of 15 minutes or more with no reason. Refusing the build instead would bring back hand-typed reasons.
- **The builder checks against the renderer's own parser.** The range and stated-moment rules live in the renderer, so the builder writes only what `parse_range_end` reads back. `RecordEtaRangeOmitted` stays reachable for an end more than a day from its ETA.
- **Older `eta_seen.json` text is adopted, not parsed.** Adoption keeps `first_seen` and `requested`, so a held unit is not asked twice.


## Recording ETAs and chained predictions

`progress_history.py eta` records the promise before the director states it.
Use `--time`, `--basis`, and an optional `--phase-id`.
A range needs both `--earliest` and `--latest` and must contain the promise.
Short times mean today in the user zone; later dates need `YYYY-MM-DDTHH:MM`.
The command refuses past times and local times that the clock skips.
It prints `ETA recorded: HH:MM <zone>`.

The `eta_stated` event names the target through `eta_phase_id`.
`phase_id` names the phase active when the director recorded the promise.
The basis is `checkpoint`, `eta-command`, or `stated`.
An ETA recorded before its phase starts still belongs to that phase.
An unstarted phase with no instance keeps events whose `eta_phase_id` names it in its `eta_trail`.
A phase without an instance accepts a promise only after all earlier instances with its phase id finish.

`build_plan` combines phase headings, durable run events, and stated promises.
History follows each phase's title family across instances.
It excludes retired work under a reused phase number.
`eta_trail` holds actual promises; `schedule_end` holds the prediction used for later phases.
A passed promise remains visible as late, while the schedule advances for future work.
Typical completed-phase durations and gaps predict the remaining phases.
The Units table formats these facts and sorts unit names without case distinctions.
Progress shows only a bar: `🟩` done, `🟨` open, and `⬜` not started.
It uses at most 20 equal groups, with open before not started before done.
All three characters use the same display width, and the bar has no text cut.
Phase shows `<done>/<total>` from the same phase rows, or `—` with no phases.
Notes follows Phase and contains the open phase's id, step, and task, or its title without a step.
Without an open phase, Notes shows the plan state.
Repair rounds append `fix rounds <n>[ + 1 running]` in Notes when the count exceeds zero.
Notes joins parts with ` · ` and cuts text at 80 characters with `…`.
An open run supplies Progress, Phase, and both ETA cells when the plan has no phases.
Notes uses this fallback when it has phase rows; otherwise, it keeps the plan state.
ETA Next follows Notes and shows the open plan phase's latest promise.
Otherwise, it uses the newest open phase outside the plan, then the next not-started plan phase with a promise.
A passed promise adds ` · late <elapsed>`.
Open work without a promise shows `no ETA`; no open work or promised phase gives `—`.
ETA Plan follows ETA Next only when at least one row has unfinished plan phases.
It shows the predicted plan finish without `ends`; rows without unfinished plan phases show `—`.
Both ETA cells use `HH:MM` today, `Sat 18:41` within six days, otherwise `24 Oct`, in the production zone.
The predicted finish never precedes any open or not-started plan phase's latest promise, including checkpoint promises.
The optional Machine column stays directly after Status.

## The short report

`progress_history.py progress` prints the clocks table, phase heading, ETA line, stage, and worker lines.
`--tables` adds the round table and its earlier-work line.
Verification gate lines appear in both report forms.
The ETA line shows the latest promise, its first value, moves, repair rounds, and overdue time.
It reports unavailable ETA facts explicitly.
Worker lines use each worker's live window or newest finished window.
The clocks table uses the predicted plan finish for Project ETA.
The legacy layout keeps its existing form and adds the ETA line.

`/unit:report [tables|on|off]` presents the report and copies the clocks table without changes.
`tables` adds the round table after the clocks table.
Delegation launch and result replies end with this short report.
Use `/showrunner:units` for the production's Units table.
