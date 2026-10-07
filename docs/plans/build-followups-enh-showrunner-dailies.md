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
- **Port:** none.
- **Test lanes:** `scripts/production/` (tests sit beside the scripts as `test_*.py`).
- **Test:** `python3 -m unittest discover -s scripts/production -p 'test_*.py'`, from the worktree root. While iterating, one file: `python3 -m unittest discover -s scripts/production -p '<test_file>.py'`.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes`. It exits 3 in every checkout, so the exit status says nothing.
- **Invariants:**
  - Tests never start a real `claude`, `systemd-run` or `ssh`, never write `~/.claude/config/`, `~/.local/state/` or a real production doc, and never push anywhere. A test that needs tmux uses a private server, as `test_unit_status.py` does.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - A state is a named type, never a bare optional: follow `UpcomingWork | NoUpcomingWork` in `dailies_render.py` and `PromptUnits | UnreadablePrompt` in `showrunners.py`.
  - Times state PDT.

## Phases

### Phase 1 — Retired units drop out, and a simple dailies groups idle units  · status: done

#### Work Order

**Goal:** A unit whose Units row is marked `retired` no longer appears in the status output, the dailies, the waits or the stall watch, and a `simple` dailies lists each waiting, idle unit as one line under `### Waiting and idle`.

**Spec:**

*A. Retired rows.* A Units row is retired when its Plan cell (the second cell) matches `\bretired\b`. The live row to read: `| stalls-unit | (run done; retired by the user 2026-10-07, worktree removed) follow-up: … | … | … | hook | — | … |`.

1. `add_unit.py`:
   - `RETIRED = re.compile(r"\bretired\b")`.
   - `live_unit_rows(lines: list[str]) -> list[str]` — the rows `unit_rows(lines)` returns, without the retired ones. A row with fewer than two cells stays in, so each reader's own `invalid Units row` refusal still fires.
   - `retired_sessions(lines: list[str]) -> set[str]` — `cell_value` of the Session cell (the fifth) of every retired row that has one.
2. `showrunners.py`:
   - `class CheckedDoc(NamedTuple): path: Path` and `class NoCheckedDoc(NamedTuple): reason: str`.
   - `checked_doc(instance: Path) -> CheckedDoc | NoCheckedDoc` — the production doc an instance directory's `conf` hands `production_check.sh` on its `CHECK=` line: `shlex.split` the value, take the token after the one whose basename is `production_check.sh`. A missing conf, line or token, or a relative path, is `NoCheckedDoc` with the reason. This is the lookup `stall_watch.finished_run_units` holds today (lines 133-139), moved here.
3. `live_units.py` (new), `live_units.py <showrunner session>`:
   - `live_units(session: str) -> list[str]`: the registry's unit names for that showrunner (`showrunners.load_settings()`, standby units included, registry order), without the retired sessions of its production.
   - Retired sessions: for every `showrunner-*` directory under `showrunners.NOTIFIER_STATE_DIR`, take `checked_doc`; for a `CheckedDoc` whose `production_field(lines, "Showrunner session")` equals the session, add `retired_sessions(lines)`. A doc that cannot be read or lacks the field retires nothing.
   - The CLI prints one name per line. A session absent from the registry prints `showrunner absent from config: <session>` to stderr and exits 1, the words `unit_status.sh` uses today; an unreadable registry prints its error and exits 1.
4. `unit_status.sh`: the `--showrunner` branch calls `"$REPO/scripts/lib/py" "$REPO/scripts/production/live_units.py" "$showrunner"` in place of its inline Python, and drops the `config` variable (`showrunners.py` reads `SHOWRUNNERS_CONFIG` itself). The form with unit names on the command line is unchanged. The header comment says the `--showrunner` form skips retired units.
5. `dailies_input.units_from_doc` and `waiting.units_from_doc` read `live_unit_rows(lines)`. So the builder asks for no status block and no judgment for a retired unit, and `waiting.named_unit` refuses one as `unknown unit`.
6. `stall_watch.finished_run_units` takes its doc from `showrunners.checked_doc(showrunners.NOTIFIER_STATE_DIR / f"showrunner-{runner.slug}")` and counts a row as finished when its Plan cell matches `\brun done\b` or `RETIRED`.
7. `docs/production_format.md`, under the Units table: "A retired unit keeps its row. Its Plan cell says `retired` (`(retired by the user 2026-10-07, worktree removed)`); the status script, the dailies, the waits and the stall watch then skip it. A live unit's Plan cell never uses the word."

Scope note (unit director): `merge_checkpoint.py`, `ci_points.py` and `production_lifecycle.py` keep reading every row. A retired unit sends no checkpoint, and the wrap already passes a unit whose branch is gone.

*B. The idle group.*

1. Input. A unit takes an optional `idle` object: `{"waits_for": "<what it waits for>", "until": "YYYY-MM-DDTHH:MM"}`, `until` in the report's zone. Add `"idle"` to `parse_unit`'s allowed keys.
2. Types, in `dailies_render.py`:
   ```python
   @dataclass(frozen=True)
   class IdleWait:
       waits_for: str
       until: datetime

   @dataclass(frozen=True)
   class NotIdle:
       pass
   ```
   `Unit` gains `idle: IdleWait | NotIdle`.
3. `parse_idle(fields: JsonMap, where: str) -> IdleWait | NotIdle`, at every length. Absent or `null` is `NotIdle`. Otherwise:
   - `check_keys` with exactly `waits_for` and `until`.
   - `until` must match `STARTED`, else `InputError(f"{where}.idle.until: {value!r} must be when the unit comes back, as YYYY-MM-DDTHH:MM in the zone")`.
   - `waits_for` is `text(...)`, at most `IDLE_LIMIT = 80` characters, else `InputError(f"{where}.idle.waits_for: {n} characters; at most {IDLE_LIMIT}, one short line")`; then `check_words(line, "idle.waits_for", where)`.
4. `check_idle(report: Report, now: datetime) -> None`: an `IdleWait` whose `until <= now` raises `InputError(f"units[{index}].idle.until: that time has passed; remove idle now the unit is back at work, or give the new time")`. Call it in `main` after `check_changes` and in `check_render_state` after `check_changes`, so the builder refuses before it changes the clock.
5. `idle_clock(moment: datetime, now: datetime) -> str`, by whole days between the two dates: the same day `16:33`; one to six days ahead `Sat 22:44`; seven or more `Wed 2026-10-14 07:50`. No zone: the report's first line states it.
6. `in_idle_group(length: str, unit: Unit) -> bool`: `length == "simple"`, the unit is an `IdleWait`, and nothing on it asks anyone to act: `needs_user` is false, `needed` is `None` and `held` is `None`. A unit that fails any of the three keeps its full section.
7. `render`: units in the idle group get no `### <unit>: <project>` section. After the last full unit section and before the other topics, when the group is not empty:
   ```
   ### Waiting and idle
   - cache-evict until 16:33: a day of sweep readings
   - mul_add until Sat 22:44: three nightly lint runs
   - screenshot until Wed 2026-10-14 07:50: a week of capture timings

   ```
   Each line is `- {unit.name} until {idle_clock(until, now)}: {waits_for}`, ordered by `until`, ties in report order. The timeline, the footer, the saved state and the `dailies ETAs:` line are unchanged: every unit keeps its timeline row. `page` and `elaborate` print every unit's full section and no group.
8. `commands/showrunner/dailies.md`:
   - Input: name `idle` among the judgment fields, and add its table row: "`idle` | Only while the unit waits on a clock gate, a data window or another unit and nothing of its runs: no seats, no helpers, no build or test. `waits_for` is what it waits for, one short line of at most 80 characters; `until` is when it comes back, `YYYY-MM-DDTHH:MM` in `ZONE`, from the unit director's own statement or `LOG`, never made up. Remove it once the unit works again; the renderer refuses an `until` that has passed. A unit with no known return time gets no `idle`."
   - What the renderer writes: "**Waiting and idle:** in `simple` only, each unit with `idle` and no `needed`, no unmerged checkpoint and nothing waiting on you gives up its section for one line under `### Waiting and idle`, after the other units: `- cache-evict until 16:33: a day of sweep readings`. A later day adds its weekday (`Sat 22:44`), and a week or more away its date (`Wed 2026-10-14 07:50`). It keeps its timeline row. `page` and `elaborate` keep its full section. User, 2026-10-07."
   - Length: the `simple` row's note that waiting, idle units take one line each.

**Files:**
- `scripts/production/add_unit.py` — `RETIRED`, `live_unit_rows`, `retired_sessions`
- `scripts/production/showrunners.py` — `CheckedDoc`, `NoCheckedDoc`, `checked_doc`
- `scripts/production/live_units.py` — new: a showrunner's units without the retired ones
- `scripts/production/unit_status.sh` — the `--showrunner` form calls `live_units.py`
- `scripts/production/dailies_input.py` — `units_from_doc` skips retired rows
- `scripts/production/waiting.py` — `units_from_doc` skips retired rows
- `scripts/production/stall_watch.py` — `finished_run_units` uses `checked_doc` and counts retired rows
- `docs/production_format.md` — the retired marker
- `scripts/production/test_live_units.py` — new: the cases for `live_units.py`
- `scripts/production/test_add_unit.py` — retired-row cases
- `scripts/production/test_unit_status.py` — retired-row cases
- `scripts/production/test_dailies_input.py` — retired-row cases
- `scripts/production/test_waiting.py` — retired-row cases
- `scripts/production/test_stall_watch.py` — retired-row cases
- `scripts/production/test_showrunners.py` — `checked_doc` cases
- `scripts/production/dailies_render.py` — `IdleWait`, `NotIdle`, `parse_idle`, `check_idle`, `idle_clock`, `in_idle_group`, the group in `render`
- `commands/showrunner/dailies.md` — the `idle` field and the group
- `scripts/production/test_dailies_render.py` — idle cases

**Seats:** 2 writers. Part A and part B share no file, so each seat writes its own code and tests.
- `impl` — part A: `add_unit.py`, `showrunners.py`, `live_units.py`, `unit_status.sh`, `dailies_input.py`, `waiting.py`, `stall_watch.py`, `docs/production_format.md`, `test_add_unit.py`, `test_live_units.py`, `test_unit_status.py`, `test_dailies_input.py`, `test_waiting.py`, `test_stall_watch.py`, `test_showrunners.py`; hub: `add_unit.py` (every doc reader imports it). Tests: `live_unit_rows` drops a retired row and keeps a `run done` one; `retired_sessions` reads a backticked Session cell with commentary; `checked_doc` for a good conf, a missing `CHECK=` line and a relative path; `live_units` gives the registry's units without a retired one, ignores another showrunner's doc, gives every unit when no instance names a doc, and exits 1 for an absent showrunner; `unit_status.sh --showrunner` prints no `== <unit>` block and no `SESSION GONE` for a retired unit while its other unit still prints (the harness copies `live_units.py` and `add_unit.py` beside the script); the builder builds from a status file and a judgment that both leave the retired unit out; `waiting.named_unit` refuses a retired unit; the stall watch counts a row marked `retired` without `run done` as finished.
- `test` — opens as impl. Part B: `dailies_render.py`, `commands/showrunner/dailies.md`, `test_dailies_render.py`. Tests: a `simple` report with three idle units and one working unit prints the working unit's section, then `### Waiting and idle` with three lines in `until` order showing the three clock forms, then any other topic, and each idle unit keeps its timeline row; `page` and `elaborate` print four full sections and no group; an idle unit with `needed`, with `held`, or with `needs_user` keeps its full section in `simple`; a report whose units are all idle prints only the group; the refusals: an unknown `idle` key, a malformed `until`, an 81-character `waits_for`, a plumbing word, an `until` at or before now from the renderer (exit 2) and from `check_render_state` (`StateRefused` naming `units[0].idle.until`); the `dailies ETAs:` line and the saved state still carry every idle unit.

**Constraints from prior phases:** none; this is the plan's first phase. From the earlier run (`docs/as-built/showrunner-automation.md`): `unit_status.sh` matches tmux sessions by exact name (`-t "=$u"`); `dailies_render.py` resolves each ETA once in `resolve_eta_moments`, and `render`, the chart, the state and the log line all read that result.

**Acceptance gate:** `python3 -m unittest discover -s scripts/production -p 'test_*.py'` green; `basedpyright` on each changed `.py` file ends `0 errors, 0 warnings, 0 notes`; `zsh scripts/production/unit_status.sh <dir> America/Los_Angeles --showrunner <session>` in a test tree prints no block for a retired unit; a `simple` render of an input with an `idle` unit prints `### Waiting and idle` and that unit's one line.
