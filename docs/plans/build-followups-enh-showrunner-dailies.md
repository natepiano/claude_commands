# Showrunner status: retired units and the idle group

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A retired unit drops out of the status script, the dailies, the waits and the stall watch, and a simple dailies lists units that only wait in one short group.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`

> **Production: build-followups** — unit `enh-showrunner-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via natedev, 2026-10-07 (relayed in the showrunner's words):

- "In the SIMPLE dailies only, group units that are waiting and idle (blocked on a clock gate, data window or another unit, no seats running) under one heading, one short line each: what it waits for and when it comes back online (e.g. cache-evict until 16:33, mul_add until Sat 22:44, screenshot until Wed 2026-10-14 07:50). page and elaborate keep full sections."
- "unit_status.sh lists a retired unit (stalls-unit/hook, row marked "retired") as SESSION GONE; skip rows marked retired."

## Decisions (unit director)

- **One phase, two writers.** The two changes share no file, so they run side by side and land in one checkpoint.
- **The row is the one marker.** Marking a Units row `retired` is the whole act of retiring: every reader of the Units table follows it, and the status script finds the production doc on its own, so no caller's command line changes.
- **Other files' owners.** `scripts/production/unit_status.sh`, `dailies_render.py`, `dailies_input.py`, `waiting.py`, `commands/showrunner/dailies.md` and their tests were stalls-unit's, which the user retired on 2026-10-07; the showrunner assigned them here. The checkpoint notice names each as `also touches <path> (owner stalls-unit, retired), tested against <merge branch tip>`.
- **model-study-unit** edits `agent_line` in `dailies_render.py` (the run-out text). This plan leaves `agent_line`, `agent_section` and `footer` alone. Merge `build-followups` into the branch when the showrunner says that change landed, and resolve any overlap here.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. Work in the worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner` on branch `build-followups-enh-showrunner` (unit `enh-showrunner-unit` of production `build-followups`).
- **Project started:** 2026-10-07T16:46:14.582+00:00
- **Stack:** Python 3.13, standard library only; zsh for `unit_status.sh`.
- **Layout:**
  - `scripts/production/` — production scripts and their `test_*.py`
  - `commands/showrunner/` — the showrunner commands
  - `docs/production_format.md` — the production doc format
  - `scripts/delegate/` — the `/unit:delegate` scripts and their `test_*.py`
- **Key files:**
  - `scripts/production/add_unit.py` — the production doc reader: `read_production`, `production_field`, `unit_rows`, `cell_value`. It imports `showrunners`, so `showrunners.py` never imports it.
  - `scripts/production/showrunners.py` — the registry (`config/showrunners.json`, or `SHOWRUNNERS_CONFIG`) and `NOTIFIER_STATE_DIR`.
  - `scripts/production/stall_watch.py` — `finished_run_units` (lines 130-158) finds the production doc from a notifier instance's `CHECK=` line.
  - `scripts/production/unit_status.sh` — per-unit status; its `--showrunner <session>` form reads the registry (lines 20-35).
  - `scripts/production/dailies_input.py` — the dailies input builder; `units_from_doc` (line 112), `status_blocks` (line 123).
  - `scripts/production/waiting.py` — `units_from_doc` (line 157), `named_unit`.
  - `scripts/production/dailies_render.py` — the dailies renderer: `Unit` (line 173), `parse_unit` (line 708), `check_render_state` (line 1001), `ordered_units` (line 1204), `render` (line 1229), `main` (line 1335).
  - `commands/showrunner/dailies.md` — the dailies command and its input format.
  - `docs/plans/build-followups-production.md` — a real production doc to read, never write.
  - `scripts/delegate/progress_history.py` — the run's progress recorder: `_start_activity` (line 1043), `_finish_activity`, `_close_active_activity` (line 1019), `_start_phase` (line 1167), `_finish_phase` (line 4075), `_finish_run` (line 4101), `_finding_lenses` (line 4129), `_review_trial` (line 4137).
  - `scripts/delegate/findings.py` — the findings ledger; `REVIEW_LENSES` (line 76) is `adversary`, `contract`, `craft`, `ux`.
  - `scripts/production/review_regime.py` — the showrunner's review ledger; it stores the `code N findings` number of each checkpoint notice and prints its mean per regime (line 188).
- **Port:** none.
- **Test lanes:** `scripts/production/` and `scripts/delegate/` (tests sit beside the scripts as `test_*.py`).
- **Test:** `python3 -m unittest discover -s scripts/production -p 'test_*.py'`, from the worktree root. While iterating, one file: `python3 -m unittest discover -s scripts/production -p '<test_file>.py'`.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes`. It exits 3 in every checkout, so the exit status says nothing.
- **Invariants:**
  - Tests never start a real `claude`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, and never push anywhere. A test that needs tmux uses a private server, as `test_unit_status.py` does.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - A state is a named type, never a bare optional: follow `UpcomingWork | NoUpcomingWork` in `dailies_render.py` and `PromptUnits | UnreadablePrompt` in `showrunners.py`.
  - Times state PDT.

## Phases

### Phase 1 — Retired units drop out, and a simple dailies groups idle units  · status: done

#### As-built

- **Retired rows.** A Units row is retired when its Plan cell (the second cell) matches `RETIRED = re.compile(r"\bretired\b")`. The row stays in the production doc; `unit_status.sh --showrunner`, the dailies builder, the open-waits report and the stall watch skip it. A live unit's Plan cell never uses the word.
- **Row readers in `add_unit.py`.** `live_unit_rows(lines: list[str]) -> list[str]` returns `unit_rows(lines)` without the retired rows; a row with fewer than two cells stays in, so each reader's own `invalid Units row` refusal still fires. `retired_sessions(lines: list[str]) -> set[str]` returns the `cell_value` of the Session cell (the fifth) of each retired row that has one; `retired_units(lines: list[str]) -> set[str]` returns the `cell_value` of the Unit cell (the first) of each retired row.
- **The checked doc.** `showrunners.checked_doc(instance: Path) -> CheckedDoc | NoCheckedDoc` returns the production doc that an instance directory's `conf` passes to `production_check.sh` on its `CHECK=` line: `shlex.split` the value, take the token after the one whose basename is `production_check.sh`. `CheckedDoc(path: Path)` and `NoCheckedDoc(reason: str)` are `NamedTuple`s; a missing conf, line or token, or a relative path, is `NoCheckedDoc`. `live_units` and `stall_watch.finished_run_units` both read through it.
- **Live units.** `live_units.live_units(session: str) -> list[str]` returns the registry's unit names for that showrunner (`showrunners.load_settings()`, standby units included, registry order) without the `retired_sessions` of every `showrunner-*` directory under `showrunners.NOTIFIER_STATE_DIR` whose checked doc has that session in `Showrunner session`. A doc that cannot be read or lacks the field retires nothing. The CLI prints one name per line; a session absent from the registry prints `showrunner absent from config: <session>` to stderr and exits 1, as does an unreadable registry with its error.
- **Idle input.** A dailies unit takes an optional `idle` object, `{"waits_for": "<what it waits for>", "until": "YYYY-MM-DDTHH:MM"}`, parsed at every length by `parse_idle(fields: JsonMap, where: str) -> IdleWait | NotIdle` into `Unit.idle`. `IdleWait(waits_for: str, until: datetime)` and `NotIdle()` are frozen dataclasses. Absent or `null` is `NotIdle`; otherwise the keys are exactly `waits_for` and `until`, `until` matches `STARTED`, and `waits_for` is at most `IDLE_LIMIT = 80` characters and passes `check_words`. `check_idle(report: Report, now: datetime) -> None` raises `InputError` naming `units[{index}].idle.until` when `until <= now`; `main` and `check_render_state` (as `StateRefused`) both call it after `check_changes`, so the builder refuses before it changes the clock.
- **Idle group.** `grouped_wait(length: str, unit: Unit) -> IdleWait | NotIdle` returns the unit's `IdleWait` only when `length == "simple"`, `needs_user` is false, and `needed` and `held` are both `None`; any other unit keeps its full section. `render` gives a grouped unit no `### <unit>: <project>` section and prints `### Waiting and idle` after the last full section and before the other topics, one line per unit, `- {unit.name} until {idle_clock(until, now)}: {waits_for}`, ordered by `until`, ties in report order. `idle_clock(moment: datetime, now: datetime) -> str` counts whole days between the two dates: `16:33` on the same day, `Sat 22:44` one to six days ahead, `Wed 2026-10-14 07:50` at seven or more, with no zone because the report's first line states it. The timeline, the footer, the saved state and the `dailies ETAs:` line carry every unit; `page` and `elaborate` print every full section and no group.

**Files:**
- `scripts/production/add_unit.py` — `RETIRED`, `live_unit_rows`, `retired_sessions`, `retired_units`
- `scripts/production/showrunners.py` — `CheckedDoc`, `NoCheckedDoc`, `checked_doc`
- `scripts/production/live_units.py` — `live_units` and its CLI, `live_units.py <showrunner session>`
- `scripts/production/unit_status.sh` — the `--showrunner` form takes its units from `live_units.py` and holds no `config` variable (`showrunners.py` reads `SHOWRUNNERS_CONFIG` itself); the form with unit names on the command line prints every unit named
- `scripts/production/dailies_input.py` — `units_from_doc` reads `live_unit_rows`, so the builder asks for no status block and no judgment for a retired unit; when rows exist and none is live it refuses with `no live units: every Units row is marked retired`
- `scripts/production/waiting.py` — `units_from_doc` reads `live_unit_rows`, so `named_unit` refuses a retired unit as `unknown unit`; `waits()` drops a wait whose waiting unit is in `retired_units`
- `scripts/production/stall_watch.py` — `finished_run_units` takes its doc from `checked_doc(NOTIFIER_STATE_DIR / f"showrunner-{runner.slug}")` and counts a row as finished when its Plan cell matches `\brun done\b` or `RETIRED`
- `scripts/production/dailies_render.py` — `IdleWait`, `NotIdle`, `IDLE_LIMIT`, `parse_idle`, `check_idle`, `idle_clock`, `grouped_wait`, the group in `render`
- `commands/showrunner/dailies.md` — the `idle` field and the group: `idle` only while a unit waits on a clock gate, a data window or another unit with nothing of its own running; `until` from the unit director's statement or the log; a unit with no known return time gets no `idle`
- `commands/showrunner/produce.md` — the scheduled update's line that waiting, idle units take one line each in a `simple` dailies
- `docs/production_format.md` — the retired marker, under the Units table
- `scripts/production/test_add_unit.py`, `test_live_units.py`, `test_unit_status.py`, `test_dailies_input.py`, `test_waiting.py`, `test_stall_watch.py`, `test_showrunners.py`, `test_dailies_render.py` (`IdleGroupTests`) — the cases for each of the above

**Gotchas:** `waiting.py waits` rebuilds waits from the log alone, so filtering the doc's rows never reaches it; it filters on the waiting unit's name through `retired_units`. `live_units` matches the registry's unit name against the Session cell, not the Unit cell, so a retired row with an empty or mistyped Session cell leaves the unit in the status output. `until` is a naive local time in the report's zone, compared with the renderer's naive local now. `unit_status.sh` is zsh and takes `<state-dir> <user-zone> --showrunner <session>`; run from a worktree it needs `SHOWRUNNERS_CONFIG` pointed at the real registry. `test_unit_status.py` copies `add_unit.py`, `live_units.py` and `showrunners.py` into its test tree, so a new import in `live_units.py` needs a new entry in that list.

**Ruled out:** hiding a live unit's wait on a retired unit, which would hide a stuck unit; skipping retired rows in `merge_checkpoint.py`, `ci_points.py` and `production_lifecycle.py`, which read every row because a retired unit sends no checkpoint and the wrap already passes a unit whose branch is gone.

### Phase 2 — The recorder keeps a row for closing work and counts every code finding  · status: done

#### Work Order

**Goal:** Two defects in the progress recorder, reported by screenshot-unit from its last phase (2026-10-07 10:10 PDT) and routed here by the showrunner. After the run's last phase closes, the unit director's remaining work (the plan shrink, the final gate, the style review, the as-built pass) can still open a recorded activity, where today `start-activity` exits 1. And the checkpoint notice's `code N findings` counts every code finding the phase's review raised, where today it counts only a lens that has been off since 2026-10-04 and so always says 0. A third defect, reported by the showrunner from the first live run after phase 1 merged (2026-10-07 10:45 PDT): a live unit whose Plan cell only describes work with the word `retired` was dropped as retired; the marker becomes a leading word that prose cannot trip.

**Spec:**

Part A, an activity after the last phase closes (`scripts/delegate/progress_history.py`):

- Today `_start_activity` refuses unless the phase in state is `active` (`Start a phase before starting an activity`, exit 1). `/unit:delegate`'s phase-end order closes the phase with `finish-phase` and then opens a `shrink` activity; with no next phase to start, that call fails and the shrink has no record. `verify.sh` opens its own activity the same way and hides the failure, so the final gate records nothing either.
- New rule: `start-activity` succeeds whenever the run is active and a phase has been started in it, whether that phase is still active or already closed by `finish-phase`. On a closed phase the activity's events (`activity_started`, `activity_finished`) carry that phase's `phase_instance_id`, the same phase `review-trial` reads.
- A closed phase stays closed: its status, `finished_at` and recorded elapsed time do not change when an activity opens or closes after it.
- `finish-activity` closes such an activity with its status, result and elapsed seconds, as it does inside a phase.
- `start-phase` for the next phase closes an activity still open on the closed phase as `interrupted`, as it does for one left open inside a phase. `finish-run` closes one still open as `interrupted` too.
- `start-activity` still refuses, exit 1, in two cases, each with its own message: no phase has been started in this run (keep `Start a phase before starting an activity`), and the run has finished (name that the run is finished).
- `progress` keeps its present answer on a closed phase (`No active phase to report: …`); this phase does not change what a report prints.
- Model the answer to "which phase does this activity belong to" as a named type (the house rule: a state is a named type, never a bare optional), not a `dict | None` passed around.

Part B, the code finding count (`scripts/delegate/progress_history.py`, `scripts/production/review_regime.py`):

- Today `_review_trial` adds to `code_findings` only when a `finding_opened` event names the `craft` lens. The broad review runs the `adversary` and `contract` lenses and the unit director's own read opens findings with no lens, so a phase with four code findings prints `code 0 findings`.
- New rule: a finding counts as a code finding when it names any lens other than `ux`, or names no lens at all. A finding that names `ux` and a code lens counts once in each number, as today. The `ux` count, the minutes and the printed line's form (`review trial: ux <N> findings, code <N> findings, review-seat minutes <M>, ux check minutes <U>, ux repair minutes <R>`) do not change; `scripts/production/merge_checkpoint.py` parses that line.
- The legacy lens word `both` still reads as `adversary,contract` through `_finding_lenses`, so it counts as one code finding.
- Make the words agree with the number: `_review_trial`'s docstring; in `scripts/production/review_regime.py`, the header docstring gains one sentence (from 2026-10-07 the code number is every code finding the phase's review raised; before that it was the code-quality reviewer's alone) and the report label `code reviewer findings per phase (mean)` becomes `code findings per phase (mean)`.

Part C, the retired marker (`scripts/production/add_unit.py`, `docs/production_format.md`):

- Today `RETIRED = re.compile(r"\bretired\b")` is searched anywhere in a Units row's Plan cell. This unit's own row said `follow-up: retired units drop out of the status`, so the status output, the dailies, the waits and the stall watch all skipped a live unit.
- New rule: a row is retired only when its Plan cell begins with the word `retired`, alone or straight after one opening parenthesis: `retired by the user 2026-10-07 …` or `(retired by the user 2026-10-07, worktree removed) …`. Leading spaces are ignored. The word is lower case and whole (`retiredness` does not count). The word anywhere later in the cell means nothing, and that includes later inside a leading parenthesis: `(run done; retired by the user)` is a live row.
- One place holds the rule: one name in `add_unit.py` that answers "is this Plan cell retired", used by `live_unit_rows`, `retired_sessions` and `retired_units`, and by `finished_run_units` in `scripts/production/stall_watch.py`, which today imports the pattern and searches the Plan cell itself. No other file tests the word.
- `docs/production_format.md`, the retired paragraph: say the Plan cell begins with `retired`, alone or inside an opening parenthesis, give the example `(retired by the user 2026-10-07, worktree removed)`, and say the word anywhere else in the cell changes nothing. Drop the sentence that a live unit's Plan cell never uses the word.
- Tests (`scripts/production/test_add_unit.py`): a Units table built from rows of the real production doc `docs/plans/build-followups-production.md` (copy the rows into the test; read the doc, never write it, and do not open it from the test): the stalls-unit row with its Plan cell opening `(retired by the user 2026-10-07; run done, worktree removed)`, this unit's row with the Plan cell that tripped the old rule (`… (follow-up: retired units drop out of the status and a simple dailies groups idle units; …`), and one more live row. `retired_units` is exactly `{"stalls-unit"}`, `retired_sessions` is exactly that row's session, and `live_unit_rows` keeps the other two. Also: `retired` first, `(retired` first, the word mid-cell, `(run done; retired by the user)`, `retiredness`, and `Retired`, each with its expected answer (the last four are live).
- Two fixtures written under the old rule mark a row with `(run done; retired by the user)`: `scripts/production/test_live_units.py` (line 60) and `scripts/production/test_dailies_input.py` (line 296). Change each to open with `(retired by the user; run done)` and change nothing else in those files.

Tests (`scripts/delegate/test_progress_history.py`), each driving the script as the file's other tests do:

- After `start-phase` and `finish-phase`, `start-activity` exits 0, `finish-activity` exits 0, both events carry the closed phase's instance id, and the phase's status and `finished_at` in state are unchanged.
- With no phase started, `start-activity` exits 1 with the present message; after `finish-run`, it exits 1 with the finished-run message.
- An activity left open after the last phase is closed as `interrupted` by the next `start-phase`, and by `finish-run`.
- `review-trial` for a phase whose ledger opened four findings (`adversary,contract`, `contract`, `contract`, and one with no lens) prints `code 4 findings`; a `ux` finding adds to `ux` only; `ux,craft` adds one to each; `both` adds one code finding. Update `test_review_trial_reports_the_phase_lens_counts_and_review_minutes` where its expected numbers follow the old rule.

**Files:**
- `scripts/delegate/progress_history.py`
- `scripts/production/review_regime.py`
- `scripts/delegate/test_progress_history.py`
- `scripts/production/add_unit.py`
- `docs/production_format.md`
- `scripts/production/test_add_unit.py`
- `scripts/production/test_live_units.py`
- `scripts/production/test_dailies_input.py`
- `scripts/production/stall_watch.py`
- `scripts/production/test_stall_watch.py`

**Seats:** 1 writer + 1 tester. Both defects sit in one file, so one seat writes the code and the other the tests.
- `impl` — `scripts/delegate/progress_history.py` (parts A and B) and `scripts/production/review_regime.py` (the words).
- `test` — `scripts/delegate/test_progress_history.py`: the cases listed under Tests, written from this Work Order while the writer works, then run against the writer's code.

Part C arrived after those two seats launched and shares no file with them. It runs beside them in a third seat, `fix2`, opening as a writer: `scripts/production/add_unit.py`, `docs/production_format.md`, `scripts/production/test_add_unit.py`, `scripts/production/test_live_units.py`, `scripts/production/test_dailies_input.py`, and `scripts/production/stall_watch.py` with `scripts/production/test_stall_watch.py`.

**Constraints from prior phases:** Parts A and B share no file with phase 1. Part C repairs phase 1's marker: `live_unit_rows`, `retired_sessions` and `retired_units` in `add_unit.py` are its three readers in that file; `finished_run_units` in `stall_watch.py` reads the marker too, and every other script reaches it through those three. Other units run this recorder from `~/.claude/scripts/delegate/` while this phase edits the worktree's copy, so nothing here may change an event's existing fields, the state file's existing keys, or any line `progress` prints: only `start-activity`'s refusal rule and `review-trial`'s code number change. Tests write only under a temporary session directory.

**Acceptance gate:** `python3 -m unittest discover -s scripts/delegate -p 'test_progress_history.py'` green; `python3 -m unittest discover -s scripts/production -p 'test_merge_checkpoint.py'` green (it parses the review line); `python3 -m unittest discover -s scripts/production -p 'test_*.py'` green; `basedpyright` on each changed `.py` file ends `0 errors, 0 warnings, 0 notes`. Live check by the unit director: in a scratch session directory, `start-run`, `start-phase`, `finish-phase`, then `start-activity --label shrink` and `finish-activity` both exit 0.
