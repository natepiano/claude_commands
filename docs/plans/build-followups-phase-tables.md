# phase-tables

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Each production unit keeps one Markdown note in the vault with its current phase, its ETA and a table of every phase's start and finish; the dailies chart takes each unit's start and ETA from the same events.

> **Production: build-followups** — unit `phase-tables-unit`; production doc `docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` (natepiano/claude_commands) — the commands, docs and scripts every Claude and Codex session runs. Work only in the worktree `/home/natepiano/worktrees/claude-build-followups-phase-tables`, branch `build-followups-phase-tables`.
- **Project started:** 2026-10-08T15:33:07.973+00:00
- **Stack:** Python 3.10+ (stdlib only, `unittest`), zsh, Markdown command files. basedpyright must report 0 errors and 0 warnings; no file-level ignores; no `Any`.
- **Layout:** `scripts/delegate/` (the progress recorder and this plan's script), `scripts/production/` (showrunner scripts), `commands/unit/`, `commands/showrunner/`, `docs/`.
- **Key files:**
  - `scripts/delegate/progress_history.py` — the recorder. Session state in `<session-dir>/progress_history_state.json`; durable events in `~/.local/state/plan-delegate/runs/<run-id>.jsonl` (`_history_root()`, env override). Plan headings: `PHASE_HEADING_PATTERN`, `PHASE_STATUS_PATTERN`, `PHASE_TITLE_PATTERN`, `_count_plan_phases`, `plan_phases`. ETA maths: `_eta_seconds`, `eta_band_seconds` (3496), `percent_spread` (3510), `_recorded_report`. Transitions: `_start_phase` (1223), `_progress` (3948), `_finish_phase` (4186), `_finish_run` (4212). Run lookup: `_run_started_event` (732), `plan_runs` (744). Event append: `_append_event` (378). Clock: `now_epoch` (honours `PLAN_DELEGATE_NOW_EPOCH`).
  - `scripts/delegate/test_progress_history.py` — its tests; they run the recorder in temp dirs.
  - `scripts/delegate/findings.py` — precedent for a second script reading `progress_history_state.json`.
  - `scripts/production/add_unit.py` — `read_production(path) -> Production` (`slug`, `showrunner_session`, `zone`), `production_field`, `cell_value`, the Units table readers (`live_unit_table`). `scripts/production/merge_checkpoint.py` — `parse_units` (each unit's `plan` and `worktree`). `scripts/production/unit_lookup.py` — a unit's live session name.
  - `scripts/whoami/agent_notes.py` — precedent for a script writing vault notes: temp file, keep mode, `os.replace` (`set_fields`, line 81).
  - `commands/unit/report.md` — `<ProgressReport/>`, read at every tick. `commands/unit/eta.md`, `commands/unit/eta_breakdown.md` — where a unit states an ETA.
  - `scripts/production/dailies_input.py` — the dailies input builder (`eta_state` 221, `eta_value` 252, `run` 300). `commands/showrunner/dailies.md` — its input contract.
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
  - Do not edit `commands/unit/delegate.md` or `scripts/delegate/verify.sh`: build-report-unit's unmerged branch changes both (the showrunner, 2026-10-08). If a phase cannot avoid one, tell the showrunner before editing.
  - Gate G2 cleared on 2026-10-08 and the merge branch is merged into this branch (`d4d9a27`). Nothing is built for the Mac until the user answers (the showrunner, 2026-10-08).
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

### Phase 2 — The note is written to the vault at every report, named for the unit's session  · status: todo

#### Work Order

**Goal:** Every production unit's note appears in the vault and stays current: the recorder rewrites it at each phase start, progress report, phase finish and run finish, it is named for the unit's session as it is now, and a note left under an old name is removed. Nothing but the note is stored; the table is rebuilt from the recorder's events each time it is asked for.

**Spec:**

1. Names first. Both seats build on them, and by themselves they change no behaviour, no rendered word and no JSON key.
   - Recorder: `eta_band_seconds(percent, elapsed, spread) -> EtaBand | EtaProjectionUnavailable`. `EtaBand(NamedTuple)` has `remaining`, `earliest`, `latest` in seconds: the three numbers the tuple held, in that order. `EtaProjectionUnavailable(NamedTuple)` has no fields. Every caller in `progress_history.py` and `phase_table.py` matches on the type; no `None` return remains.
   - Table: each state name says its subject. `Reported`→`ReportedPhaseProgress`, `NotReported`→`PhaseProgressNotReported`, `Completed`→`CompletedPhaseTiming`, `NotRecorded`→`PhaseTimingNotRecorded`, `Predicted`→`PredictedPhaseTiming`, `NotPredicted`→`PhaseTimingNotPredicted`, `NoEta`→`EtaUnavailable`, `InstanceStillRunning`→`PhaseInstanceWithoutFinish`, `RunningPhase`→`OpenPhase`, `NoPhaseRunning`→`NoOpenPhase`.
2. `build_plan(plan_path: Path) -> PhaseRecord` becomes the one builder, and the events are its authority:
   - The open phase is the newest phase instance that has a `phase_started` and no `phase_finished` in the plan's newest run (`plan_runs` returns oldest first), unless that run has a `run_finished` event. A `phase_finished` of any status (`completed`, `stopped`, `error`) closes its instance. An unfinished instance in an older run is not open.
   - Open means started and not finished in the events. It does not prove the unit's session is alive: a session that died leaves its phase open. `OpenPhase`'s doc comment says so, and callers decide liveness (the note is written by the running recorder; the dailies builder has its own unit state).
   - The session state file is not read for the phase. `_start_phase` writes state before it appends `phase_started`, so a phase present in state with no start event does not exist for the table.
   - `build(session_dir)` reads the state file for the plan path only (`project_plan_doc`, else `plan_doc`, as now) and returns `build_plan` of it. `NoState` remains for a missing or unreadable state file. `NoPlan` is raised only when the plan file cannot be read, and its message names the path. A plan with no recorded run, or with no open phase, is a normal record with `NoOpenPhase`.
   - The open row's `started` is the earliest start among the open instance and the earlier instances of that plan phase that carry its recorded title.
   - An open phase whose id and title match no plan phase stays as now: the current-phase heading shows it and no table row is marked running.
   - Upcoming phases are predicted whenever a typical duration is known (the median of completed rows, else the open phase's projected total, as now). The first prediction starts from the open phase's ETA when it has one; else from the later of now and the open phase's start plus the typical duration; else, with no open phase, from now. With no typical duration, rows stay `PhaseTimingNotPredicted` and the plan finish `UnknownFinish`.
   - One named state carries the open phase through the builder: no placeholder `datetime`, no loose per-field locals.
3. `phase_table.py refresh --session-dir <dir>`:
   - Read the plan's `> **Production: <name>** — unit `<unit>`; production doc `<path>`` line (format: `docs/delegate_plan_format.md`). No such line: exit 0 silently; a run outside a production writes nothing (author's scope call: the user's layout is `showrunners/<showrunner>/`).
   - Resolve the production doc against the plan's Git root; `read_production` (`scripts/production/add_unit.py`) gives `slug`, `showrunner_session`, `zone`. Its `Refusal` is a reason for the failure line below.
   - Vault root: `PHASE_TABLE_VAULT`, default `~/rust/hanadocs/showrunners`. When its parent (`~/rust/hanadocs`) does not exist, exit 0 and write nothing.
   - Note path: `<vault root>/<showrunner_session>/<file name>.md`. `file name` is `unit_lookup.marked_units(slug)[unit].claude.name` when that is a `LiveClaude` with a non-empty name; in every other case (no marked tmux session, `ClaudeNotRunning`, `ClaudeUnknown`, `OSError`) the unit id. In the name, `/` and NUL become `-` and a leading `.` is dropped. Nothing stores the name; the removal rule below deletes the note written under the previous one. Read `unit_lookup.py`; change nothing in it.
   - Imports: `phase_table.py` already puts the repository root on `sys.path` and imports `from scripts.delegate import progress_history`. Import the production readers the same way (`from scripts.production import unit_lookup`, `from scripts.production.add_unit import Refusal, read_production`) and put `scripts/production` on `sys.path` first, because `add_unit.py` imports its siblings by bare name. The form must type-check under the repository's `pyrightconfig.json` with no ignore and must run from any working directory.
   - Note content: frontmatter, then `# <file name>`, then `render(record, zone)`. Frontmatter is exactly three keys, in this order: `phase_table: true`, `production`, `unit`. They are how a later refresh recognises this unit's own notes; every other fact is in the body.
   - Write through a temp file in the same directory and `os.replace`; create directories as needed; mode `0o644`. Before replacing, the target must be absent or carry this unit's three ownership keys; otherwise write nothing, print the reason to stderr and exit 1. A hand-written note is never overwritten, and two names that become one path cannot take each other's note.
   - Before the first write on a machine, when the vault root sits inside a Git checkout (by default `~/rust/hanadocs`) and that checkout's `info/exclude` has no `showrunners/` line, append that line. Find the file through the containing checkout, never under `showrunners/`. Never edit the vault's `.gitignore` (the showrunner's call, 2026-10-08: it changes no shared file and needs no commit there).
   - Then remove every other `*.md` under `<vault root>/*/` whose frontmatter has `phase_table: true` and this `production` and `unit`, and remove a showrunner directory left empty. Never touch a file without `phase_table: true`.
   - `NoState`, `NoPlan` or a `Refusal`: print the reason to stderr and exit 1. `show` does the same for the first two.
4. No phase-table record file is written and no record directory exists (the user's rule, 2026-10-08: "only the least necessary things written down to files - anything that is quickly discoverable by a script should be done that way"). `show --json` stays as the on-request form.
5. Recorder hook: `_refresh_phase_table(session_dir)` in `progress_history.py`, called as the last act of `_start_phase`, `_progress` (every path that printed a report), `_finish_phase` and `_finish_run`. It returns at once unless the state's plan file contains `> **Production:`. Otherwise it runs `[sys.executable, <phase_table.py beside it>, "refresh", "--session-dir", …]` with a 10 s timeout, stdout discarded. On non-zero exit, timeout or `OSError` it prints one line to stderr, `phase table not written: <reason>`, and the recorder's own stdout and exit status are unchanged. `refresh` never calls the recorder's CLI and never takes its session lock: the recorder holds that lock for the whole command the hook runs in.
6. `commands/unit/report.md`, step 5: one paragraph — the `progress` call also rewrites this unit's phase note; nothing is written by hand; when the user asks for the phase table, paste `phase_table.py show --session-dir "${SESSION_DIR}" --zone <User zone>`.
7. `docs/as-built/plan-delegate-progress-history.md`: add the hook to the description of the four commands, and `eta_band_seconds`'s two results.

**Files:**
- `scripts/delegate/phase_table.py` — the renamed states, `build_plan`, the prediction start, `refresh`, the note's name, stale-note removal.
- `scripts/delegate/test_phase_table.py` — builder and refresh cases.
- `scripts/delegate/progress_history.py` — `EtaBand | EtaProjectionUnavailable`, `_refresh_phase_table` and its four call sites.
- `scripts/delegate/test_progress_history.py` — band and hook cases.
- `commands/unit/report.md` — the step 5 paragraph.
- `docs/as-built/plan-delegate-progress-history.md` — the hook and the band's results.

**Seats:** 2 writers — the table and the recorder each keep their tests beside them; they meet only at `eta_band_seconds`'s result and the `refresh` command line, and the Spec fixes both.
- `impl` — `scripts/delegate/phase_table.py`, `scripts/delegate/test_phase_table.py`. Builder cases: a stopped phase is not open; a finished run has no open phase; a phase left unfinished in an older run is not open once a newer run exists; a phase in the state file with no `phase_started` event is not open; a run whose first line cannot be parsed is skipped; an open phase outside the plan shows in the heading only; with no report yet, predictions start from the start plus the typical duration, or from now when that has passed; with no open phase they start from now; with no typical duration nothing is predicted. Refresh cases, with `PHASE_TABLE_VAULT` in a temp dir and the lookup's own test stand-ins (the override `tmux_binary()` reads, and `NOTIFIER_SESSIONS_DIR`): the note's exact bytes; the live session's name is the file name, else the unit id; a renamed session moves the note; a changed showrunner name moves it and removes the empty directory; a hand-written note in the same folder survives; a hand-written file at the target path is left unchanged with exit 1; another unit's note at the target is left unchanged with exit 1; a plan with no Production line writes nothing; nothing but the note is created under the vault root or the recorder's history root; the exclude line is added once, in the containing checkout; `show` and `refresh` print the reason and exit 1 for a missing state file and an unreadable plan; `phase_table.py show --help` exits 0 from a working directory outside the repository.
- `test` — opens as impl: `scripts/delegate/progress_history.py`, `scripts/delegate/test_progress_history.py`, `commands/unit/report.md`, `docs/as-built/plan-delegate-progress-history.md`. Cases: both results of `eta_band_seconds`; a failing `refresh` leaves the stdout and exit status of `start-phase`, `progress`, `finish-phase` and `finish-run` byte-identical and prints the one stderr line; a timeout prints it too; a plan with no Production line starts no subprocess; and, once the table seat's `refresh` is in the tree (ask on the board), one `progress` call on a production plan writes the note well inside the timeout.

**Constraints from prior phases:** Phase 1 built `build`, `render`, `show`, `_json_record`, and the recorder's public `plan_phases`, `plan_runs` (a run matches by its own `plan_doc`), `eta_band_seconds`, `now_epoch`, `resolve_plan_path`, `percent_spread`. The record is the `PhaseRecord` NamedTuple of named states; under their phase 1 names they are `DonePhase | RunningPhase | TodoPhase`, `Completed | NotRecorded`, `Predicted | NotPredicted`, `Reported | NotReported`, `ProjectedEta | NoEta`, `FinishedAt | PredictedFinish | UnknownFinish`, `RunningPhase | NoPhaseRunning`. Past work is attached to a plan phase by recorded title, then id (`_phase_instances`, `_instances_by_phase`); keep that rule. The table reader takes a shared lock on each run file while it reads its events; `plan_runs` reads each run's first line with no lock. The recorder holds its exclusive lock on a run file only inside `_append_event`, and its session lock for the whole command. `scripts/production/unit_lookup.py` is in this branch as merged (`216bc9a`): `marked_units(slug) -> dict[str, MarkedUnit]`; `MarkedUnit.claude` is `LiveClaude` (`name`, `session_id`), `ClaudeNotRunning` or `ClaudeUnknown`. For an unchanged input, `show`'s Markdown and `--json` bytes change only where a prediction now replaces a dash.

**Acceptance gate:** both Test commands green; basedpyright clean on the changed files; after one `/unit:report` in this run, the note exists under `~/rust/hanadocs/showrunners/natedev/`, named for this unit's live session (`phase-tables-unit.md` when its tmux session is not marked), with this plan's table; no phase-table record file exists under the recorder's state directory.

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
- `impl` — `scripts/delegate/progress_history.py`, `scripts/delegate/test_progress_history.py`, `commands/unit/eta.md`, `commands/unit/eta_breakdown.md`, `docs/as-built/plan-delegate-progress-history.md`. Cases: each refusal; no active phase; an exact ETA's event has no range keys and a ranged one has both; the note is rewritten.
- `test` — opens as impl: `scripts/delegate/phase_table.py`, `scripts/delegate/test_phase_table.py`, with `eta_stated` events written straight into the test's run files. Cases: a stated ETA wins over a later progress report; a passed one yields to the projection and `first` stays; a new phase starts with none; `first` holds across a restatement; exact and ranged rows; a stated ETA with no progress report; predictions start from a stated ETA.

**Constraints from prior phases:** Phase 2's `_refresh_phase_table`, `build_plan` and state names (`OpenPhase`, `ReportedPhaseProgress`, `EtaUnavailable`), and the `--json` form's `current.eta` object. No phase-table record file exists. The recorder holds its session lock for the whole `eta` command, as for every other.

**Acceptance gate:** both Test commands green; basedpyright clean on the changed files; in this run, under `TZ=America/Los_Angeles`, `eta --time <a time 30 min ahead> --basis "check"` changes the note's ETA rows and `show --json`'s `current.eta.source` to `stated`.

### Phase 4 — The dailies take each unit's start and ETA from its run's records  · status: todo

#### Work Order

**Goal:** The dailies chart takes each unit's phase start and ETA from the same events as the unit's note, so no ETA line has to be on the captured screen or typed into the status file; a retired unit's note is removed.

**Spec:**

1. `phase_table.py show --production-doc <doc> --json`, a new form (`show` takes exactly one of `--session-dir` and `--production-doc`): one JSON object keyed by unit id, with one entry per live Units row. A unit's plan is its Plan cell resolved against its Worktree cell when relative, carried as a named `ProductionUnitPlan(unit, plan)`: `plan_runs` matches a run by its absolute plan path, and a unit's plan lives in the unit's worktree, not the showrunner's checkout. Use the Units readers in `scripts/production` (`merge_checkpoint.parse_units` for Plan and Worktree, `add_unit.live_unit_table` for which rows are live). An entry is the unit's `--json` record, or `{"unavailable": "<reason>"}` when its plan cannot be read. A unit with no recorded run has a record with no open phase. Times are in the doc's User zone.
2. `dailies_input.py` runs that command once per build (the file's `command` helper) and parses the answer into one named state per unit: `RecordBackedPhase(number, started, eta)` when the record has an open phase whose id is all digits and belongs to the plan, else `NoRecordedOpenPhase`. A failed or unparsable call is reported as `phase tables: failed — <reason>`, every unit takes today's path, and the build does not stop.
3. Per unit the record is used as one bundle or not at all:
   - The judgment's `phase` stays required and stays the showrunner's words (`dailies.md`: `Phase <N> of <M>: <what it changes>`, the oldest phase not yet merged).
   - When the judgment's phase number equals the record's open phase number, `started` and the ETA come from the record. A value the judgment gives for any of those fields wins.
   - In every other case the unit takes today's path unchanged, pane ETA lines included: a held checkpoint heads the unit while its next phase is open, a `follow-up` phase, a suffixed id such as `12a`, an open phase outside the plan, no open phase, an unavailable record.
4. A record-backed ETA enters the existing ETA states (`EtaFresh | EtaStale | EtaPassed | EtaNone`) through one new function beside `eta_state`, never by composing pane text:
   - Its age counts from `stated_at` for a stated ETA and from `as_of` for a projected one. Older than one hour: `EtaStale` with that time as `first_seen` (`detail: set HH:MM`). A time already passed: `EtaPassed`. No ETA in the record: `EtaNone`. With `held` given and the ETA unchanged since the builder first saw it: `EtaFresh`, never requested.
   - It keeps the same `eta_seen.json` record under the same `<unit>|<phase text>` key (`text`, `first_seen`, `requested`), so `eta_requested`, the once-per-phase `request /unit:eta: <unit>` line and `merge_eta_records` work unchanged.
   - It fills `eta.time`, `eta.earliest`, `eta.latest` (`HH:MM`, `+1` for tomorrow), `eta.percent`, and `eta.first` (`YYYY-MM-DDTHH:MM`). The renderer requires `percent` with `time`: with no reported percent in the record and none in the judgment, the ETA is not taken from the record and takes today's path.
   - `eta.why`: when the judgment gives none, a stated ETA supplies its `basis`. `eta.fixes` stays the judgment's. Read `check_render_state` in `dailies_render.py` before writing this: the built input must pass it on a first build, an unchanged one and one where the time moved by 15 minutes or more.
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
- `impl` — `scripts/production/dailies_input.py`, `scripts/production/test_dailies_input.py`, `commands/showrunner/dailies.md`. Cases, each over consecutive builds with a stand-in for the command's JSON: first sight; unchanged; moved by 15 minutes with and without a judgment `why`; stale after an hour with one request line; passed; stated, projected and no ETA; a status capture with no ETA text at all; a held checkpoint heading the unit while its next phase is open; a judgment value wins; no percent; a failed call.
- `test` — opens as impl: `scripts/delegate/phase_table.py`, `scripts/delegate/test_phase_table.py`. Cases: a plan resolved against the unit's worktree with the showrunner's checkout elsewhere; an unreadable plan gives `unavailable` for that unit only; retired rows are left out; prune across every showrunner directory; hand-written notes survive.

**Constraints from prior phases:** `build_plan`, the `PhaseRecord` states and the `--json` shape (Phases 1–3, `current.eta` with `source`, `stated_at`, `as_of`, `basis`, `first`), `PHASE_TABLE_VAULT`, and the note's three ownership keys. No phase-table record file is read or written; the builder asks each time. `dailies_input.py` and `test_dailies_input.py` are enh-showrunner-unit's files, in this branch as merged at `216bc9a`: name them in the checkpoint notice as `also touches`, and trial-merge that unit's tip first (`<ProductionUnit/>` item 9). Re-read the file before composing; Key files' line refs are from that merge.

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
