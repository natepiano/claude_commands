# phase-tables

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Each production unit keeps one Markdown note in the vault with its current phase, its ETA and a table of every phase's start and finish; the dailies chart takes each unit's start and ETA from the same events.

> **Production: build-followups** — unit `phase-tables-unit`; production doc `docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` (natepiano/claude_commands) — the commands, docs and scripts every Claude and Codex session runs. Work only in the worktree `/home/natepiano/worktrees/claude-build-followups-phase-tables`, branch `build-followups-phase-tables`.
- **Project started:** 2026-10-08T15:33:07.973+00:00
- **Stack:** Python 3.10+ (stdlib only, `unittest`), zsh, Markdown command files. basedpyright must report 0 errors and 0 warnings; no file-level ignores; no `Any`.
- **Layout:** `scripts/delegate/` (the progress recorder and this plan's script), `scripts/production/` (showrunner scripts), `commands/unit/`, `commands/showrunner/`, `docs/`.
- **Key files:**
  - `scripts/delegate/progress_history.py` — the recorder. Session state in `<session-dir>/progress_history_state.json`; durable events in `~/.local/state/plan-delegate/runs/<run-id>.jsonl` (`_history_root()`, env override). Plan headings: `PHASE_HEADING_PATTERN`, `PHASE_STATUS_PATTERN`, `PHASE_TITLE_PATTERN`, `_count_plan_phases`, `plan_phases`. ETA maths: `_eta_seconds`, `eta_band_seconds` (3554), `percent_spread` (3568), `_recorded_report`. Transitions: `_start_phase` (1279), `_progress` (4244), `_finish_phase` (4250), `_finish_run` (4277). Run lookup: `_run_started_event` (788), `plan_runs` (800). Event append: `_append_event` (434). Note hook: `_refresh_phase_table` (391). Clock: `now_epoch` (honours `PLAN_DELEGATE_NOW_EPOCH`).
  - `scripts/delegate/test_progress_history.py` — its tests; they run the recorder in temp dirs.
  - `scripts/delegate/findings.py` — precedent for a second script reading `progress_history_state.json`.
  - `scripts/production/add_unit.py` — `read_production(path) -> Production` (`doc`, `slug` and `zone` among its fields; the showrunner's session name is not in the doc: `current_name(slug)` in `scripts/production/showrunners.py` looks it up, and answers empty while the showrunner is not running), `production_field`, `cell_value`, the Units table readers (`live_unit_table`). `scripts/production/merge_checkpoint.py` — `parse_units` (each unit's `plan` and `worktree`). `scripts/production/unit_lookup.py` — a unit's live session name (`marked_units`) and its run state from the run records (`run_state(worktree) -> UnitState`). `scripts/production/fake_showrunner.py` — the tests' stand-in for a running showrunner (`write_timer`, `write_session`).
  - `scripts/whoami/agent_notes.py` — precedent for a script writing vault notes: temp file, keep mode, `os.replace` (`set_fields`, line 81).
  - `commands/unit/report.md` — `<ProgressReport/>`, read at every tick. `commands/unit/eta.md`, `commands/unit/eta_breakdown.md` — where a unit states an ETA.
  - `scripts/production/dailies_input.py` — the dailies input builder (`units_from_doc` 115, `eta_state` 221, `eta_value` 252, `command` 271, `run` 300). `commands/showrunner/dailies.md` — its input contract.
  - `docs/as-built/plan-delegate-progress-history.md` — the recorder's as-built.
- **Test lanes:** `scripts/delegate/` and `scripts/production/` — tests sit beside the code as `test_<module>.py` (`unittest`).
- **Build:** none (Python).
- **Test:** `python3 -m unittest discover -s scripts/delegate -p 'test_phase_table.py'` and `python3 -m unittest discover -s scripts/delegate -p 'test_progress_history.py'`; Phase 4 adds `python3 -m unittest discover -s scripts/production -p 'test_dailies_input.py'`.
- **Lint:** `basedpyright <each changed .py>` — pass is its `0 errors, 0 warnings` line (it exits 3: pyrightconfig names a `.venv` no checkout has).
- **Invariants:**
  - The table writer never costs a report: no failure in it changes the recorder's output or exit status (user: agents must not re-read or re-write the note, and a report must not break).
  - No session name is stored. A unit is keyed by production slug and unit id; a name is only ever a file's name, recomputed at each write (the user's lookup ruling, 2026-10-08, relayed by the showrunner).
  - Scripts own the note's format. No command file tells an agent to write or edit the note.
  - Tests never write to the real vault or state directory: every root has an env override, and tests set it.
  - Every time shown is in the production doc's **User zone**.
  - Forbidden-words hooks apply to code, comments and prose.
  - Do not edit `commands/unit/delegate.md` or `scripts/delegate/verify.sh`: build-report-unit's unmerged branch changes both (the showrunner, 2026-10-08). If a phase cannot avoid one, tell the showrunner before editing. `commands/unit/direct.md`, and every line in any file that names `/unit:delegate` or `/unit:direct`, are delegate-rename-unit's: leave them as they are.
  - Gate G2 cleared on 2026-10-08 and the merge branch is merged into this branch (`d4d9a27`, and again at `619f104` as `0fe7f62`). Nothing is built for the Mac until the user answers (the showrunner, 2026-10-08).
  - Only two things are stored: the note, and a stated ETA as one event in the recorder's run file. Everything a script can find again is rebuilt from the recorder's events when asked (the user, 2026-10-08: "only the least necessary things written down to files - anything that is quickly discoverable by a script should be done that way").

## Phases

### Phase 1 — The phase table, built from the run's records  · status: done

#### As-built

- `scripts/delegate/phase_table.py` reads the recorder's events and writes none. `build(session_dir: Path) -> PhaseRecord` raises `NoState` or `NoPlan`; `render(record: PhaseRecord, zone: ZoneInfo) -> str` returns Markdown; `show(session_dir: Path, zone: ZoneInfo, json_output: bool = False) -> str` returns either form. CLI: `phase_table.py show --session-dir <dir> [--zone <IANA>] [--json]`, zone defaulting to the machine's, exit 1 with one line on stderr.
- `PhaseRecord` is a NamedTuple of named states, never optional fields: `plan: Path`, `updated: datetime`, `current: RunningPhase | NoPhaseRunning`, `phases: list[DonePhase | RunningPhase | TodoPhase]`, `plan_finish: FinishedAt | PredictedFinish | UnknownFinish`. Done times are `Completed | NotRecorded`, todo times `Predicted | NotPredicted`, running progress `Reported | NotReported`, a report's ETA `ProjectedEta | NoEta`, a recorded instance's finish `InstanceFinished | InstanceStillRunning`. Times are aware `datetime`s; `None` appears only in the JSON.
- Recorded work attaches to a plan phase by recorded title (casefold, backticks removed, whitespace collapsed), then by id, across every run of the plan; a done row takes its times from the title family of its last completed instance, and a todo row never shows recorded times. The running phase is the state's `phase` while `active`; its ETA is its last `progress_reported` event's time plus `eta_band_seconds`, absent with no report or percent 0. Later todo phases chain from that ETA: start = previous finish + gap, finish = start + typical, with typical the median seconds of the plan's completed phases (else the running phase's projected total) and gap the median from a completed finish to the next start in the same run (0 with no sample).
- `render` gives a heading (`**Phase 3 of 6 — <title>**`, or `**No phase running — <done> of <total> done**`), a summary table (`Started`, `Done`, `ETA`, `Plan finish`, `Updated`; no `ETA` row when no phase runs) and a phase table (`Phase`, `What it delivers`, `Status`, `Start`, `Finish`) in descending plan order. Times are `MM-DD HH:MM` in `zone`, the zone abbreviation appears once on `Updated`, and an unknown cell is `—`. `--json` prints `_json_record(record, zone)`: `plan`, `updated`, `plan_finish`, `current` (its `eta` is `{time, earliest, latest, source: "projected"}` or `null`) and `phases`, with ISO-8601 offset times.
- `progress_history.py` public readers: `plan_phases(plan_path: Path) -> list[PlanPhase]` (`PlanPhase` TypedDict: `id: str`, `title: str`, `done: bool`; document order; one classification shared with `_count_plan_phases`; a title drops its status marker and a trailing commit annotation and keeps a descriptive parenthetical), `plan_runs(plan_path: Path) -> list[Path]` (oldest first; a run matches by its own `run_started.plan_doc` only), `eta_band_seconds(percent: int, elapsed: int, spread: float) -> tuple[int, int, int] | None` (`(eta, low, high)` seconds remaining; `_eta_band_cells` formats from it), and `now_epoch`, `resolve_plan_path`, `percent_spread`.

**Files:**
- `scripts/delegate/phase_table.py` — builds, renders and prints one unit's phase table.
- `scripts/delegate/test_phase_table.py` — its tests.
- `scripts/delegate/progress_history.py` — the public readers; what the recorder writes is unchanged.
- `scripts/delegate/test_progress_history.py` — the recorder's tests, with cases for the readers.

**Binds later work:** `_json_record` is the only JSON shape and nothing stores it. `NoState` and `NoPlan` carry the user-facing reason; the vault-note phase's `refresh` reports each as `phase table not written: <reason>`. Todo rows are `NotPredicted` and an unfinished plan's finish is `UnknownFinish` (both dashes) unless the running phase has a `ProjectedEta`; the vault-note phase adds a prediction start for the no-report and no-running-phase cases. An active phase with no plan heading shows in the heading line only, with no row; the dailies phase leaves such a unit on its existing path.

**Gotchas:** The reader takes a shared lock on each run file and the recorder appends under an exclusive lock on the same file, so a read started while the recorder holds its lock blocks; `plan_runs` reads each run's first line with no lock. `_start_phase` writes session state before it appends `phase_started`: the events, not the state file, say what happened. A phase left open by a session that died is indistinguishable in the events from live work. Recorded history reuses phase numbers and rewords headings: of 219 recorded phase starts, 177 match a plan heading by id and title, 29 by id only, 1 by title only, 12 not at all (ad hoc and follow-up ids).

**Ruled out:** a stored JSON record per unit (store only what no script can find again); grouping events by phase id alone (history reuses phase numbers); seat slots renamed to subsystem names (the plan format fixes `impl` and `test`).

### Phase 2 — The note is written to the vault at every report, named for the unit's session  · status: done

#### As-built

- **Builder** (`scripts/delegate/phase_table.py`): `build_plan(plan_path: Path) -> PhaseRecord` is the one builder, and the recorder's events are its authority. The open phase is the newest phase instance with a `phase_started` and no `phase_finished` in the plan's newest run (`plan_runs` returns oldest first), unless that run has a `run_finished` event. A `phase_finished` of any status (`completed`, `stopped`, `error`) closes its instance, and an unfinished instance in an older run is not open. The session state file is never read for the phase: `_start_phase` writes state before it appends `phase_started`, so a phase in state with no start event does not exist for the table.
- `build(session_dir)` reads the state file for the plan path only (`project_plan_doc`, else `plan_doc`) and returns `build_plan` of it. `NoState` is a missing or unreadable state file; `NoPlan` is raised only when the plan file cannot be read, and its message names the path; a plan with no recorded run, or with no open phase, is a normal record with `NoOpenPhase`. The open row's `started` is the earliest start among the open instance and the earlier instances of that plan phase that carry its recorded title. An open phase whose id and title match no plan phase shows in the current-phase heading and marks no table row running. One named state carries the open phase through the builder; there is no placeholder `datetime`.
- **States**, each named for its subject: `OpenPhase(phase, title, started, progress) | NoOpenPhase`, `ReportedPhaseProgress(percent, eta) | PhaseProgressNotReported`, `CompletedPhaseTiming | PhaseTimingNotRecorded`, `PredictedPhaseTiming | PhaseTimingNotPredicted`, `ProjectedEta(time, earliest, latest) | EtaUnavailable`, `InstanceFinished | PhaseInstanceWithoutFinish`, `OpenPhaseInstance`, `TypicalDuration | UnknownDuration`, `PlanPhasePosition | OutsidePlan`. Open means started and not finished in the events, never proof that the unit's session is alive (a session that died leaves its phase open); `OpenPhase`'s doc comment says so, and each caller decides liveness.
- **Predictions:** upcoming phases are predicted whenever a typical duration is known (the median of completed rows, else the open phase's projected total). The first prediction starts from the open phase's ETA when it has one; else from the later of now and the open phase's start plus the typical duration; else, with no open phase, from now. With no typical duration, rows stay `PhaseTimingNotPredicted` and the plan finish `UnknownFinish`.
- **`phase_table.py refresh --session-dir <dir>`** reads the plan's ``> **Production: <name>** — unit `<unit>`; production doc `<path>` `` line (format: `docs/delegate_plan_format.md`); with no such line it exits 0 silently and writes nothing. It resolves the production doc against the plan's Git root; `read_production` (`scripts/production/add_unit.py`) gives `slug` and `zone`, and `showrunners.current_name(slug)` (`scripts/production/showrunners.py`) gives the showrunner's session name. The vault root is `PHASE_TABLE_VAULT`, default `~/rust/hanadocs/showrunners`; when its parent (`~/rust/hanadocs`) does not exist, `refresh` exits 0 and writes nothing.
- **Note path:** `<vault root>/<the showrunner's session name>/<file name>.md`. While `current_name` answers empty (the showrunner is not running, or has no update timer), the note is rewritten in the folder of the unit's most recently modified owned note, and the first refresh after the showrunner runs again moves it to the new name's folder; with no owned note `refresh` refuses with `the showrunner of <slug> is not running` and writes nothing, the exclude line included. A lookup `OSError` is a one-line refusal too.
- **Note name and content:** `file name` is `unit_lookup.marked_units(slug)[unit].claude.name` when that is a `LiveClaude` with a non-empty name; in every other case (no marked tmux session, `ClaudeNotRunning`, `ClaudeUnknown`, `OSError`) the unit id. In the name, `/` and NUL become `-` and a leading `.` is dropped. Neither name is stored; `unit_lookup.py` is unchanged. The note is frontmatter of exactly three keys in this order (`phase_table: true`, `production`, `unit`), then `# <file name>`, then `render(record, zone)`. The three keys are how a later refresh recognises this unit's own notes; every other fact is in the body.
- **Publication:** directories are created as needed and the note's mode is `0o644`. An existing target is replaced (temp file in the same directory, then `os.replace`) only when it carries this unit's three ownership keys; a hand-written file or another unit's note at the target is left unchanged, the reason goes to stderr and the exit status is 1, so two names that become one path cannot take each other's note. A first write is published with `os.link`, so a file that appears at the target meanwhile is refused with `phase note appeared before publication: <path>` and never replaced.
- **Exclude line and stale notes:** before the first write on a machine, when the vault root sits inside a Git checkout whose `info/exclude` lacks the line, `refresh` appends the vault root's path inside the checkout plus `/` (`showrunners/` for the default vault); nothing is added when the vault root is the checkout root. The file is found through the containing checkout, never under `showrunners/`. After the write, every other `*.md` under `<vault root>/*/` whose frontmatter has `phase_table: true` and this `production` and `unit` is removed, with any showrunner directory left empty; a file without `phase_table: true` is never touched. A renamed unit session or showrunner moves the note by this rule.
- **Failures and storage:** `NoState`, `NoPlan` or a `Refusal` prints the reason to stderr and exits 1 in `refresh`; `show` does the same for the first two. No phase-table record file or record directory exists: the table is rebuilt from the events on each request, and `show --json` is the on-request form.
- **Recorder** (`scripts/delegate/progress_history.py`): `eta_band_seconds(percent, elapsed, spread) -> EtaBand | EtaProjectionUnavailable`. `EtaBand(NamedTuple)` has `remaining`, `earliest`, `latest` in seconds; `EtaProjectionUnavailable(NamedTuple)` has no fields; every caller in `progress_history.py` and `phase_table.py` matches on the type, and no `None` return remains.
- **Hook:** `_refresh_phase_table(session_dir)` is the last act of `_start_phase`, `_progress` (every path that printed a report), `_finish_phase` and `_finish_run`. It returns at once unless the state's plan file contains `> **Production:`, and returns quietly when that file is not valid UTF-8. Otherwise it runs `[sys.executable, <phase_table.py beside it>, "refresh", "--session-dir", …]` with a 10 s timeout and stdout discarded. On non-zero exit, timeout or `OSError` it prints one stderr line, `phase table not written: <reason>`, and the recorder's own stdout and exit status are unchanged: a report never breaks because the note failed. `refresh` never calls the recorder's CLI and never takes its session lock, which the recorder holds for the whole command the hook runs in.

**Files:**
- `scripts/delegate/phase_table.py` — the builder, the renamed states, the prediction start, `refresh`, note naming, ownership, stale-note removal.
- `scripts/delegate/test_phase_table.py` — builder and refresh cases, with `PHASE_TABLE_VAULT` in a temp dir and `scripts/production/fake_showrunner.py` supplying the showrunner.
- `scripts/delegate/progress_history.py` — `EtaBand | EtaProjectionUnavailable`, `_refresh_phase_table` and its four call sites.
- `scripts/delegate/test_progress_history.py` — band and hook cases, and one end-to-end `progress` call that runs `refresh`.
- `commands/unit/report.md` — step 5: the `progress` call also rewrites this unit's phase note, nothing is written by hand, and a requested phase table is `phase_table.py show --session-dir "${SESSION_DIR}" --zone <User zone>`.
- `docs/as-built/plan-delegate-progress-history.md` — the hook in the description of the four commands, and `eta_band_seconds`'s two results.

**Binds later work:**
- The stated-ETA phase builds on `OpenPhase`, `ReportedPhaseProgress`, `ProjectedEta`, `EtaUnavailable`, `OpenPhaseInstance`, `_reported_eta`, `_last_progress`, `_newest_open_instance` and `_json_record`'s `current.eta` object, and matches on `EtaBand | EtaProjectionUnavailable`. `_last_progress` still returns an optional, which that phase replaces with named states, stated events included; the prediction start becomes a stated ETA when one exists. A test there that expects a note needs a running showrunner from `scripts/production/fake_showrunner.py`. Its live check runs the recorder from the branch's checkout, because the copy installed under `~/.claude` has the note hook but no `eta` command until that phase is promoted, and records the phase's real ETA, because the recorded value shows in the live note and the dailies.
- The dailies phase: `Production` has `slug` and `zone` only. `unit_lookup.run_state(worktree) -> UnitState` reads run state from the same run records; the dailies phase keeps `StatusBlock.state` and does not call `run_state`. `live_unit_table` is its one Units reader. It reads the open phase as `RecordBackedPhase` (percent and ETA states) or `PhaseRecordNotUsable` (with its reason), with a matching phase number as the gate, tested in `scripts/production/test_dailies_input.py`, and uses `eta.why` for a projected ETA. Its `prune` removes a retired unit's note and keeps hand-written ones (`test_phase_table.py`); `prune` and `show --production-doc` never need the showrunner's name, and nothing ahead depends on the showrunner folder's name.

**Gotchas:**
- `scripts/production/showrunners.py` reads `NOTIFIER_STATE_DIR` and `NOTIFIER_SESSIONS_DIR` once, at import: a test that runs `refresh` in a subprocess sets them in the child's environment, and a test that calls `phase_table.main()` in the test process patches the module constants.
- `add_unit.py` imports its siblings by bare name, so `phase_table.py` puts `scripts/production` on `sys.path` before `from scripts.production import unit_lookup` and `from scripts.production.add_unit import Refusal, read_production`; the form type-checks under `pyrightconfig.json` with no ignore and runs from any working directory.
- A vault on a file system without hard links refuses a note's first write with one line.
- Recorded history reuses phase ids across runs of a plan, so `_plan_finish` looks at every row, not only the last.
- Nothing removes or renames the note of a production that has ended: a later unit that takes the same session name under another production is refused at every report, because the target exists and belongs to another unit. No remaining phase changes this.
- The recorder's test suite takes two to four minutes (195 s measured); the table suite about six seconds.

**Ruled out:** reading the showrunner's name from the production doc (`Production.showrunner_session` and the doc's `Showrunner session` line no longer exist); a stored session or showrunner name, or a cache of the lookup; a phase-table record file (anything a script discovers quickly is not stored); editing the vault's `.gitignore` (a shared file, where `info/exclude` needs no commit); renaming the dailies builder's ETA states or restructuring the renderer's `Eta` here (another unit's code); refusing a dailies build when a projected ETA moved (it would bring back hand-typed reasons).

### Phase 3 — A stated ETA is recorded  · status: todo

#### Work Order

**Goal:** When a unit states an ETA with `/unit:eta` or `/unit:eta_breakdown`, the note carries that time, its range and its basis until it passes.

**Spec:**

1. Recorder subcommand `progress_history.py eta --session-dir <dir> --time <YYYY-MM-DDTHH:MM> [--earliest <…> --latest <…>] --basis <text>`. Times are local to the process `TZ` (units run the recorder under the User zone, `<ProductionUnit/>` item 11). It requires an active phase, appends an `eta_stated` event (`eta_at`, `basis`, and with a range `eta_earliest_at` and `eta_latest_at`; all times as epochs) through `_append_event`, calls `_refresh_phase_table`, and prints `ETA recorded: <HH:MM zone>`. With no range the event carries neither range key. A time in the past, `--earliest` after `--time`, or `--latest` before it is refused with exit 2. One of `--earliest` / `--latest` without the other is refused. This event is the only stored copy of a stated ETA: no script can find one again, so it is the one thing this plan stores beside the note.
2. Table states:
   - The ETA moves from `ReportedPhaseProgress`, which keeps `percent` only, to `OpenPhase.eta: StatedEta | ProjectedEta | EtaUnavailable`, because a stated ETA exists with no progress report.
   - `StatedEta(time, range: EtaRange | NoEtaRange, stated_at, basis)`; `EtaRange(earliest, latest)`. Never optional fields on `ProjectedEta`.
   - `ProjectedEta` gains `as_of`: the time of the progress report it was projected from.
   - `OpenPhase.first_stated: FirstStatedEtaTarget | EtaNeverStated`. `FirstStatedEtaTarget(time)` is the `eta_at` of the instance's first `eta_stated` event: the time first promised, not when it was said. It stays after a stated ETA passes and the projection returns.
   - The open phase's ETA is its instance's last `eta_stated` while that `eta_at` is later than now; otherwise the projection; otherwise `EtaUnavailable`. The builder's prediction start takes the ETA from either source.
   - `--json`: `current.eta` is `{time, earliest, latest, source, stated_at, basis, as_of, first}` with `source` `"stated"` or `"projected"`; a key with no value is `null`.
   - No optional carries an event through the builder. `_last_progress`, which returns `dict[str, object] | None` today, returns `LastPhaseProgressEvent | PhaseProgressEventNotRecorded`; the instance's stated events are read as `LatestStatedEtaEvent | NoStatedEtaEvent`; the builder matches on those types.
3. `render`: with no range the ETA row shows the time alone, with no parentheses. A second row follows it: `| ETA from | stated 09:50: <basis> |` or `| ETA from | projected from 60% done |`.
4. `commands/unit/eta.md`: before sending the answer, run the `eta` command with the time, range and basis being sent; the answer line is unchanged. `commands/unit/eta_breakdown.md`: when the breakdown moves the ETA, run the same command with the bullets' last end time.
5. `docs/as-built/plan-delegate-progress-history.md`: the `eta` command and the `eta_stated` event.

**Files:**
- `scripts/delegate/progress_history.py` — the `eta` subcommand.
- `scripts/delegate/test_progress_history.py` — its cases.
- `scripts/delegate/phase_table.py` — the ETA states, the stated ETA in the builder, the `ETA from` row.
- `scripts/delegate/test_phase_table.py` — their cases.
- `commands/unit/eta.md`, `commands/unit/eta_breakdown.md` — the recorder call.
- `docs/as-built/plan-delegate-progress-history.md` — the command and event.

**Seats:** 2 writers — the recorder writes the event and the table reads it; the event's shape in Spec 1 is all they share.
- `impl` — `scripts/delegate/progress_history.py`, `scripts/delegate/test_progress_history.py`, `commands/unit/eta.md`, `commands/unit/eta_breakdown.md`, `docs/as-built/plan-delegate-progress-history.md`. Cases: each refusal; no active phase; an exact ETA's event has no range keys and a ranged one has both; the note is rewritten; a refresh that fails, times out or cannot be launched leaves the appended `eta_stated` event, the `ETA recorded: …` line on stdout and exit 0 unchanged, with the one `phase table not written:` line on stderr.
- `test` — opens as impl: `scripts/delegate/phase_table.py`, `scripts/delegate/test_phase_table.py`, with `eta_stated` events written straight into the test's run files. Cases: a stated ETA wins over a later progress report; a passed one yields to the projection and `first` stays; a new phase starts with none; `first` holds across a restatement; exact and ranged rows; a stated ETA with no progress report; predictions start from a stated ETA.

**Constraints from prior phases:** The vault-note phase shipped, in `phase_table.py`: `build_plan(plan_path) -> PhaseRecord`; `OpenPhase(phase, title, started, progress: ReportedPhaseProgress | PhaseProgressNotReported)`; `ReportedPhaseProgress(percent, eta: ProjectedEta | EtaUnavailable)`; `ProjectedEta(time, earliest, latest)`; `OpenPhaseInstance`, the one state that carries the open phase through the builder; `_newest_open_instance`, `_last_progress` and `_reported_eta`; and `_json_record`, whose `current.eta` is `{time, earliest, latest, source: "projected"}` or `null`. In the recorder: `eta_band_seconds(...) -> EtaBand | EtaProjectionUnavailable`, and `_refresh_phase_table(session_dir)`, which returns at once unless the plan has a `> **Production:` line, runs `phase_table.py refresh` with a 10 s timeout, and on failure prints only `phase table not written: <reason>` to stderr. No phase-table record file exists. The recorder holds its session lock for the whole `eta` command, as for every other. `refresh` takes the note's folder from `showrunners.current_name(slug)`, so a test that expects a note gives the production a running showrunner: `fake_showrunner.write_timer(notifier_dir, slug, session_id, zone, doc)` and `fake_showrunner.write_session(sessions_dir, name, session_id)` (it returns a socket to close at cleanup), with `NOTIFIER_STATE_DIR` and `NOTIFIER_SESSIONS_DIR` set in the child's environment; `test_progress_refresh_writes_the_production_phase_note` in `test_progress_history.py` is the model. The recorder's test suite takes two to four minutes: run it once, after the last edit. The recorder installed under `~/.claude` has the note hook (the vault-note phase is on main as `6409d9c`) but no `eta` command until the showrunner promotes this phase, so the live check of `eta` runs this worktree's `scripts/delegate/progress_history.py`.

**Acceptance gate:** both Test commands green; basedpyright clean on the changed files; in this run, under `TZ=America/Los_Angeles`, this worktree's `scripts/delegate/progress_history.py eta` with the phase's real ETA and its basis changes the note's ETA rows and `show --json`'s `current.eta.source` to `stated` (the unit director runs it; a made-up time would show in the live note and the dailies).

### Phase 4 — The dailies take each unit's start and ETA from its run's records  · status: todo

#### Work Order

**Goal:** The dailies chart takes each unit's phase start and ETA from the same events as the unit's note, so no ETA line has to be on the captured screen or typed into the status file; a retired unit's note is removed.

**Spec:**

1. `phase_table.py show --production-doc <doc> --json`, a new form (`show` takes exactly one of `--session-dir` and `--production-doc`): one JSON object keyed by unit id, with one entry per live Units row. A unit's plan is its Plan cell resolved against its Worktree cell when relative, carried as a named `ProductionUnitPlan(unit, plan)`: `plan_runs` matches a run by its absolute plan path, and a unit's plan lives in the unit's worktree, not the showrunner's checkout. Use `add_unit.live_unit_table` with `cell_value` as the one source of each live row's Unit, Plan and Worktree; `merge_checkpoint.parse_units` also returns retired rows, so do not join the two. An entry is the unit's `--json` record, or `{"unavailable": "<reason>"}` when its plan cannot be read. A unit with no recorded run has a record with no open phase. Times are in the doc's User zone.
2. `dailies_input.py` runs that command once per build (the file's `command` helper) and parses the answer into one named state per unit: `RecordBackedPhase(number, started, percent: ReportedPhasePercent | PhasePercentUnavailable, eta: RecordedStatedEta | RecordedProjectedEta | RecordedEtaUnavailable)` when the record has an open phase whose id is all digits and belongs to the plan, else `PhaseRecordNotUsable(reason: NoOpenPhase | OpenPhaseOutsidePlan | NonNumericPhase | UnitRecordUnavailable)`. A failed or unparsable call is reported as `phase tables: failed — <reason>`, every unit takes today's path, and the build does not stop.
3. Per unit, a matching phase number is the one gate for using the record:
   - The judgment's `phase` stays required and stays the showrunner's words (`dailies.md`: `Phase <N> of <M>: <what it changes>`, the oldest phase not yet merged).
   - When the judgment's phase number equals the record's open phase number, `started` and the ETA come from the record. A value the judgment gives for any of those fields wins. A record with no reported percent still gives `started`; only its ETA takes today's path (item 4).
   - In every other case the unit takes today's path unchanged, pane ETA lines included: a held checkpoint heads the unit while its next phase is open, a `follow-up` phase, a suffixed id such as `12a`, an open phase outside the plan, no open phase, an unavailable record.
4. A record-backed ETA enters the existing ETA states (`EtaFresh | EtaStale | EtaPassed | EtaNone`) through one new function beside `eta_state`, never by composing pane text:
   - Its age counts from `stated_at` for a stated ETA and from `as_of` for a projected one. Older than one hour: `EtaStale` with that time as `first_seen` (`detail: set HH:MM`). A time already passed: `EtaPassed`. No ETA in the record: `EtaNone`. With `held` given and the ETA unchanged since the builder first saw it: `EtaFresh`, never requested.
   - It keeps the same `eta_seen.json` record under the same `<unit>|<phase text>` key (`text`, `first_seen`, `requested`), so `eta_requested`, the once-per-phase `request /unit:eta: <unit>` line and `merge_eta_records` work unchanged.
   - It fills `eta.time`, `eta.earliest`, `eta.latest` (`HH:MM`, `+1` for tomorrow), `eta.percent`, and `eta.first` (`YYYY-MM-DDTHH:MM`). The renderer requires `percent` with `time`: with no reported percent in the record and none in the judgment, the ETA is not taken from the record and takes today's path.
   - `eta.why`: the judgment's wins; else a stated ETA supplies its `basis`; else a projected ETA supplies `projected from <N>% done`, the note's own words, so a projection that moved by 15 minutes or more passes `check_changes` in `dailies_render.py` with no typed reason. `eta.fixes` stays the judgment's. Read `check_render_state` in `dailies_render.py` before writing this: the built input must pass it on a first build, an unchanged one and one where the time moved by 15 minutes or more.
   - The pane's ETA lines are not read for a record-backed unit, so its status capture needs no `Phase ETA:` line (the showrunner's requirement, 2026-10-08: the capture seldom holds a unit's ETA answer, and hana wrote each unit's line into the status file by hand).
5. `phase_table.py prune --production-doc <doc>`: remove every `phase_table: true` note under `<vault root>/*/` whose `production` is this doc's slug and whose `unit` is not a live row of its Units table, and remove a directory left empty. `dailies_input.py` runs it once per build; a failure is reported as `phase tables: failed — <reason>` and does not stop the build.
6. `commands/showrunner/dailies.md`: Gather step 3 and the Input table's `started` and `eta` rows say that a unit with a recorded open phase gets both from its run's records, that a judgment value wins, and that no `Phase ETA:` line is needed in the status capture.

**Files:**
- `scripts/delegate/phase_table.py` — `show --production-doc`, `ProductionUnitPlan`, `prune`.
- `scripts/delegate/test_phase_table.py` — their cases.
- `scripts/production/dailies_input.py` — the one call, the bundle rule, the record-backed ETA state, the prune call.
- `scripts/production/test_dailies_input.py` — cases.
- `commands/showrunner/dailies.md` — the passages.

**Seats:** 2 writers — the split is by directory; the two meet only at the `--json` shape, which phases 1 to 3 fix.
- `impl` — `scripts/production/dailies_input.py`, `scripts/production/test_dailies_input.py`, `commands/showrunner/dailies.md`. Cases, each over consecutive builds with a stand-in for the command's JSON: first sight; unchanged; moved by 15 minutes with and without a judgment `why`; stale after an hour with one request line; passed; stated, projected and no ETA; a status capture with no ETA text at all; a held checkpoint heading the unit while its next phase is open; a judgment value wins; no percent (the record still gives `started`); a projected ETA that moved by 15 minutes with no judgment `why`; a unit with an open phase in its record whose status block says `SESSION GONE` or `CLAUDE NOT RUNNING` keeps that state and its place under flags first; a failed call.
- `test` — opens as impl: `scripts/delegate/phase_table.py`, `scripts/delegate/test_phase_table.py`. Cases: a plan resolved against the unit's worktree with the showrunner's checkout elsewhere; an unreadable plan gives `unavailable` for that unit only; retired rows are left out; prune across every showrunner directory; hand-written notes survive.

**Constraints from prior phases:** `build_plan`, the `PhaseRecord` states and the `--json` shape (Phases 1–3, `current.eta` with `source`, `stated_at`, `as_of`, `basis`, `first`), `PHASE_TABLE_VAULT`, and the note's three ownership keys. No phase-table record file is read or written; the builder asks each time. `dailies_input.py`, `test_dailies_input.py` and `commands/showrunner/dailies.md` are this unit's files for this phase (the production doc's Units row), in this branch as merged at `619f104` (they did not change after `216bc9a`, so Key files' line refs hold). `dailies_render.py` is another unit's: read it, change nothing in it. Re-read the builder before composing. `Production` (`add_unit.py`) has no showrunner-session field (this phase uses its `doc`, `slug` and `zone`): the showrunner's session name is looked up, never read from the doc. A note sits in the folder named for the showrunner's looked-up name, or, while the showrunner is not running, in the folder the unit's newest note is already in; `show --production-doc` and `prune` never look the showrunner up, and `prune` walks `<vault root>/*/`. An open phase in the record is open in the events, not proof of a live session: keep `StatusBlock.state` (`Running | SessionGone | ClaudeNotRunning`) and the existing flags-first output as they are, and do not call `unit_lookup.run_state`; `build_plan` already handles a finished or absent run. A test that writes a note gives the production a running showrunner with `fake_showrunner.write_timer` and `fake_showrunner.write_session` (`NOTIFIER_STATE_DIR`, `NOTIFIER_SESSIONS_DIR`).

**Acceptance gate:** all three Test commands green; basedpyright clean on the changed files; a dailies input built for this production from a status capture with no ETA line for this unit carries this unit's `started` and `eta.time` from its run's records.

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
