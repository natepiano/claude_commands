# phase-tables follow-ups

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A production's wrap, and the dailies' prune of a retired unit, move its generated phase notes into the vault's `archive/` folder, which the vault's own auto-commit keeps, and the dailies take a phase's repair-round count from the run's records instead of from the showrunner's judgment.

> **Production: build-followups** — unit `phase-tables-unit`; production doc `docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` (natepiano/claude_commands) — the commands, docs and scripts every Claude and Codex session runs. Work only in the worktree `/home/natepiano/worktrees/claude-build-followups-phase-tables`, branch `build-followups-phase-tables`.
- **Project started:** 2026-10-10T01:56:09.135+00:00
- **Stack:** Python 3.10+ (stdlib only, `unittest`), zsh, Markdown command files. basedpyright must report 0 errors and 0 warnings; no file-level ignores; no `Any`; a dict with known keys is a `TypedDict`, a state is a named `NamedTuple`.
- **Layout:** `scripts/delegate/` (the recorder and the phase-table script), `scripts/production/` (showrunner scripts), `commands/showrunner/`, `docs/as-built/`.
- **Key files:**
  - `docs/as-built/phase-tables.md` — how the phase table, the note and the dailies record work now. Read it first in every phase.
  - `scripts/delegate/phase_table.py` — builds a `PhaseRecord` from run events, renders it, and writes, moves and prunes the vault notes. Ownership: `NoteOwnership(production, unit)` (276) read by `_note_ownership(path)` (1165) from the note's frontmatter (`phase_table: true`, `production`, `unit`); `NoteWithoutPhaseTableOwnership` for anything else, a hand-written file included. Writes: `_write_note` (1264; temp file, mode `0o644`, `os.replace` for an owned target, `os.link` for a first write; a target with other keys is refused with `phase note target is not owned by <slug>/<unit>: <path>`). Removal: `_remove_stale_notes` (1303), `prune` (1326). Vault root: `_vault_root()` (1319; `PHASE_TABLE_VAULT`, default `~/rust/hanadocs/showrunners`). Every scan is `vault_root.glob("*/*.md")`, one folder deep. CLI: `_build_parser` (1420), `main` (1438); `Refusal`, `NoState`, `NoPlan` print one stderr line and exit 1. Record states: `FirstStatedEtaTarget(time)` (80), `EtaNeverStated` (86), `OpenPhase` (121), `OpenPhaseInstance` (196); builder `_newest_open_instance` (420), `_latest_stated_eta` (596); JSON `_json_record` (1005).
  - `scripts/delegate/test_phase_table.py` — its tests. Helpers: `environment(at)` (132; sets `PHASE_TABLE_VAULT`, `PLAN_DELEGATE_HISTORY_DIR`, `PLAN_DELEGATE_NOW_EPOCH`, the notifier and tmux stand-ins to temp paths), `write_run` (87), `phase_event`, `write_units_production` (165), `run_prune` (241), `write_phase_note(folder, name, production, unit)` (258). The prune cases (`test_prune_*`, 2400 on) and the refusal case `test_refresh_refuses_to_replace_another_units_note` (2124) are the models for Phase 1's cases.
  - `scripts/delegate/progress_history.py` — the recorder; this plan does not change it. `start-pass --pass-kind {impl,test,fix,review} --fix-pass <N>` (4539–4540) records a seat's pass; `_event` (1034) stamps `phase_instance_id`, `pass_kind` and `fix_pass` on every event of an open pass (1078–1083), the `pass_started` event included (1385–1388). Round 0 is the phase's opening work; every `fix_pass` from 1 up is a repair round (`_round_entries`, the comment near 2973). `scripts/delegate/implement.sh` passes its 8th argument as `--fix-pass`.
  - `scripts/production/production_lifecycle.py` — `load|open|promote-main|wrap`. `command(step, cwd, *args)` (68) runs a process and raises `LifecycleStop(step, "failed", …)` on a non-zero exit; `report(step, state, detail)` (from `merge_checkpoint.py`, 182) prints `<step>: <state> — <detail>`. `wrap` (326): the running-production branch (338–392) ends with `notifier.sh remove showrunner-<slug>` (386–389) and the doc's status line set to `wrapped` (390–392); a doc already `wrapped` resumes at the doc commit and push. It imports its siblings by bare name and does not import `sys` today.
  - `scripts/production/test_production_lifecycle.py` — runs the script in temp Git repos with `HOME` set to a temp dir (`setUp`, 60); `running()` (146), `side_commit` (175), `run_lifecycle` (160), `assert_step` (165); `test_wrap_without_ci_retires_units_removes_notifier_and_reports` (627) is the model wrap case.
  - `scripts/production/add_unit.py` — `read_production(path) -> Production` (159; `slug` is the doc's file name less `-production.md`), `live_unit_table`, `Refusal`.
  - `scripts/production/dailies_input.py` — the dailies input builder. Its own record states: `FirstStatedEtaTarget(time)` (143), `EtaNeverStated` (147), `NumberedOpenPlanPhase` (151). `unit_phase_state` (451) decodes one unit's `show --production-doc --json` entry; `record_eta_value` (612) builds a record-backed `eta` (sets `first` at 650–651, then `result.update(fields)` at 657 so every judgment field wins); `run` (692) calls it only when the judgment's `eta` holds neither `time` nor `none` (791–805). `phase_table_path()` (486) honours `DAILIES_PHASE_TABLE`.
  - `scripts/production/test_dailies_input.py` — its tests; a stand-in table script answers from `DAILIES_PHASE_TABLE_JSON` (92–101, 160–162); `open_phase_record(…, first=…)` (281) writes one unit's record; exact `eta` dict assertions at 446–450 and 517–521; a judgment `fixes` case at 524–537.
  - `scripts/production/dailies_render.py` — reads `eta.fixes` (624–628: a whole number from 0, only with a `time`) and prints `<first> (now +H:MM, N fix rounds added)` (`drift_text`, 1033–1045). Not changed by this plan.
  - `commands/showrunner/dailies.md` — the judgment contract; the `eta.first`, `eta.fixes` row is line 190.
- **Test lanes:** `scripts/delegate/` and `scripts/production/` — tests sit beside the code as `test_<module>.py` (`unittest`).
- **Build:** none (Python).
- **Test:** `python3 -m unittest discover -s scripts/delegate -p 'test_phase_table.py'` (about six seconds); `python3 -m unittest discover -s scripts/production -p 'test_production_lifecycle.py'`; `python3 -m unittest discover -s scripts/production -p 'test_dailies_input.py'`. Each phase's gate names the ones it runs. The recorder's suite (`test_progress_history.py`, two to four minutes) is not run: neither phase changes the recorder.
- **Lint:** `basedpyright <each changed .py>` — pass is its `0 errors, 0 warnings` line (it exits 3: pyrightconfig names a `.venv` no checkout has).
- **Invariants:**
  - Tests use temp dirs only: they never touch `~/rust/hanadocs`, `~/.local/state` or `~/.claude/sessions` (the showrunner's brief for this plan, 2026-10-09). Every root has an env override (`PHASE_TABLE_VAULT`, `PLAN_DELEGATE_HISTORY_DIR`, `NOTIFIER_STATE_DIR`, `NOTIFIER_SESSIONS_DIR`, `DAILIES_PHASE_TABLE`), and tests set each one they reach.
  - A note is moved, replaced or removed only when its frontmatter carries `phase_table: true`, a `production` and a `unit`. A hand-written file is never changed (as-built invariant).
  - Scripts own the note's format. No command file tells an agent to write, move or edit a note.
  - Only two things are stored: the note, and a stated ETA as one `eta_stated` event. Everything a script can find again, a repair-round count included, is rebuilt from the recorder's events on each request (the user, 2026-10-08: "only the least necessary things written down to files - anything that is quickly discoverable by a script should be done that way").
  - No session name is stored. A unit is keyed by production slug and unit id (the user's lookup ruling, 2026-10-08).
  - In the dailies a judgment value always wins, and a judgment `time` or `none` decides the ETA entirely. `scripts/production/dailies_render.py` is not changed to suit a record (as-built invariant).
  - File ownership (production doc, Units row `phase-tables-unit`): this unit owns `scripts/delegate/phase_table.py` with its test, `scripts/production/dailies_input.py` with its test, and `commands/showrunner/dailies.md`. `scripts/production/production_lifecycle.py` and its test have no owner unit (an "also touches" file). `commands/showrunner/produce.md` is a hub file owned by enh-showrunner-unit (the doc's hub table): Phase 1 adds one clause there and names it in its checkpoint report. `docs/as-built/showrunner-automation.md` is enh-showrunner-unit's; Phase 1 adds one clause to its wrap bullet, as this unit's earlier dailies phase did to its ETA bullets.
  - Forbidden-words hooks apply to code, comments and prose.

## Phases

### Phase 1 — A wrapped production's notes are archived  · status: done

#### As-built

- `_archive_note(candidate: Path, vault_root: Path, slug: str) -> Path` moves one generated note to `<vault>/archive/<showrunner folder>/<slug>/<name>.md` (`<vault>` is `_vault_root().parent`) by `os.link` then `unlink`, never replacing a file: a taken name gets `<stem>-2.md`, `<stem>-3.md`, … An `OSError` from the `mkdir`, link or unlink becomes `Refusal("cannot archive phase note …")`; a failed unlink removes the new link first. It removes a showrunner folder the move left empty.
- `archive(production_doc: Path) -> list[Path]` (CLI `phase_table.py archive --production-doc <doc>`, one archived path per stdout line, exit 0 also when nothing moved) archives every `NoteOwnership` note of the production's slug, live or retired unit alike; hand-written notes and other productions' notes stay. A missing vault parent returns `[]`.
- `prune` archives each retired unit's note through `_archive_note` instead of deleting it; its failure surfaces in the dailies as `phase tables: unavailable — <reason>`.
- `production_lifecycle.py wrap` runs a `phase-notes` step (`sys.executable` on `PHASE_TABLE`) after the notifier removal and before the doc is marked `wrapped`; it reports `archived N phase note(s)` or `no phase notes to archive`, and a failure exits 2 and leaves the doc `running`, so a rerun of `wrap` repeats the archive.
- After an archive, a same-named unit of another production gets a fresh note at its first `refresh`.

**Files:**
- `scripts/delegate/phase_table.py` — `_archive_note`, `archive`, its subparser; `prune` archives.
- `scripts/production/production_lifecycle.py` — `PHASE_TABLE`, the `phase-notes` wrap step.
- `scripts/delegate/test_phase_table.py`, `scripts/production/test_production_lifecycle.py` — archive, prune and wrap cases.
- `commands/showrunner/produce.md`, `commands/showrunner/dailies.md`, `docs/as-built/phase-tables.md`, `docs/as-built/showrunner-automation.md` — the archive at wrap and in `prune`.

**Gotchas:**
- The archive sits outside `showrunners/`, which keeps it out of every `vault_root.glob("*/*.md")` scan and inside the vault's Git history; widening those globs would pull archived notes back into `refresh`, `prune` and `archive`.
- The `showrunners/` line in the vault's `info/exclude` is unanchored, so an archive path whose slug or showrunner folder is literally `showrunners` is ignored by Git too.
- `refresh`'s `_remove_stale_notes` still deletes the older copy of a moved unit's note; the newer note carries the same phases.

### Phase 2 — The dailies count a phase's repair rounds from the run's records  · status: todo

#### Work Order

**Goal:** a dailies input built with no `eta.fixes` in the judgment carries, for a record-backed unit with a first stated ETA, the count of repair rounds started after that ETA was first stated.

**Spec:**

Why: after the dailies phase, `eta.fixes` is the one ETA number the showrunner still types (`commands/showrunner/dailies.md` 190: "Give `fixes` every report: the repair rounds started after the first ETA"); the recorder already holds each repair pass and the first stated ETA's time.

Definition: a repair round of a phase instance is a `fix_pass` number of 1 or more on that instance's `pass_started` events (`phase_instance_id` matches). It started at the earliest `timestamp_epoch` among its `pass_started` events. It counts when that start is strictly later than the `timestamp_epoch` of the instance's first `eta_stated` event (the moment the first ETA was stated, not its `eta_at` target). Several seats of one round (a `fix` writer and a `review`, each with the same `fix_pass`) are one round. Round 0 never counts. Rounds started before the first statement do not count, even when later passes of the same round come after it.

`scripts/delegate/phase_table.py`:

- `FirstStatedEtaTarget` (80) becomes:

  ```python
  class FirstStatedEtaTarget(NamedTuple):
      """The target promised by a phase instance's first stated ETA."""

      time: datetime
      repair_rounds: int
  ```

  `repair_rounds` is the count defined above. It lives on this state because the count exists only once an ETA was stated; `EtaNeverStated` carries none, and no optional field is added.
- New helper `_repair_rounds_started_after(runs: list[list[dict[str, object]]], instance_id: str, stated_at: datetime) -> int`: walk every event of every run, keep `pass_started` events of `instance_id` whose `fix_pass` is an `int` (not `bool`) of 1 or more and whose `timestamp_epoch` reads through `_epoch`; take the earliest start per `fix_pass`; return how many of those starts are later than `stated_at`.
- `_latest_stated_eta` (596) builds the first target from the first valid `eta_stated` event as `FirstStatedEtaTarget(time=stated.time, repair_rounds=_repair_rounds_started_after(runs, instance_id, stated.stated_at))`. `stated_at` of the first event is its `timestamp_epoch`, already read at 609.
- `_json_record` (1005): `current` gains `repair_rounds_since_first_stated_eta`: the int when `first_stated` is a `FirstStatedEtaTarget`, else `null`. It sits beside `first_stated_eta_target` and is present whether or not `current.eta` is `null`.
- `render` is unchanged: the note shows no repair-round count (a scope note authored here; the item asks only for the dailies).

`scripts/production/dailies_input.py`:

- Its `FirstStatedEtaTarget` (143) gains `repair_rounds: int`.
- `unit_phase_state` (451–476): when `first_stated_eta_target` is not `null`, read `current.repair_rounds_since_first_stated_eta`; it must be an `int`, not a `bool`, of 0 or more, else `ValueError(f"{where}.current.repair_rounds_since_first_stated_eta: expected a whole number from 0")`, which `phase_tables` turns into `phase tables: unavailable — <reason>` like every other decode error. When the target is `null` the key is not read.
- `record_eta_value` (650–651): beside `first`, set `result["fixes"] = phase.first_stated.repair_rounds`. `result.update(fields)` at 657 stays after it, so a judgment `fixes` wins. No other path sets `fixes`: the early returns for `EtaPassed`, `EtaNone` and an unavailable ETA carry no `time`, and the renderer accepts `fixes` only with a `time` (`dailies_render.py` 627). A judgment `time` or `none` still bypasses `record_eta_value` entirely (791–805), so the judgment then decides `fixes` as well.

`commands/showrunner/dailies.md` line 190: replace `Give \`fixes\` every report: the repair rounds started after the first ETA.` with one sentence: a matching numbered open phase supplies `fixes` from the run record (the repair rounds started after its first stated ETA); otherwise give it every report; a judgment value wins.

`docs/as-built/phase-tables.md`: `FirstStatedEtaTarget(time, repair_rounds)` in the record block and its **ETA** bullet with the round definition; `repair_rounds_since_first_stated_eta` in the `current` JSON line; in **Dailies side → Value**, `fixes` beside `first`.

**Files:**
- `scripts/delegate/phase_table.py` — `FirstStatedEtaTarget.repair_rounds`, `_repair_rounds_started_after`, the JSON key.
- `scripts/delegate/test_phase_table.py` — round-count cases.
- `scripts/production/dailies_input.py` — decode the count; `fixes` in a record-backed `eta`.
- `scripts/production/test_dailies_input.py` — `fixes` cases; existing exact `eta` dicts that hold `first` gain `fixes`.
- `commands/showrunner/dailies.md` — the `eta.first`, `eta.fixes` row.
- `docs/as-built/phase-tables.md` — the state, the JSON key, the dailies value.

**Seats:** `1 writer + 1 tester` — the Spec fixes the definition, the JSON key and the input field, so the cases can be written before the code.
- `impl` — `scripts/delegate/phase_table.py`, `scripts/production/dailies_input.py`, `commands/showrunner/dailies.md`, `docs/as-built/phase-tables.md`; hub: none.
- `test` — writes from the Spec alone, beside the code:
  - `scripts/delegate/test_phase_table.py` (`write_run`, `phase_event`, `show --json` through the CLI with `environment(at)`): one open instance with `pass_started` events `fix_pass` 1 before the first `eta_stated`, `fix_pass` 2 after it (a `fix` and a `review` pass), `fix_pass` 3 after it, a `fix_pass` 0 pass after it, and another instance's `fix_pass` 4 after it gives `current.repair_rounds_since_first_stated_eta` `2`; a restated ETA after round 3 leaves the count at `2` (it counts from the first statement); a round whose first pass precedes the statement and whose second pass follows it does not count; with no `eta_stated` the key is `null`; with a passed stated ETA and no progress report (`current.eta` `null`) the key still carries the count.
  - `scripts/production/test_dailies_input.py`: `open_phase_record` gains `repair_rounds: int | None = 0`, written as `current.repair_rounds_since_first_stated_eta` (`null` when `first` is `None`); a record with `first` and `repair_rounds=2` and no judgment `fixes` builds `eta.fixes` `2`, and the built input passes the real renderer, whose drift text names `2 fix rounds added`; a judgment `fixes` of 5 wins over the record's 2; a judgment `time` keeps the record's count out; a record with `first` `None` builds no `fixes`; a malformed count (`-1`, `true`, `"2"`) gives `phase tables: unavailable` and the status-capture path; the exact dicts at 446–450 and 517–521 gain `"fixes": 0`.

**Constraints from prior phases:** Phase 1 adds `archive` and `_archive_note` to `phase_table.py` and its CLI, and `prune` now archives instead of deleting; this phase does not touch them. From the phase-tables run: `OpenPhase.first_stated: FirstStatedEtaTarget | EtaNeverStated` is carried through the builder by `OpenPhaseInstance` and holds across a restatement and after the stated ETA passes; `_json_record` is the only JSON shape and nothing stores it; the dailies builder writes `first` only when the record gave the time, and its record times are converted to `Production.zone`.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/delegate -p 'test_phase_table.py'` green, with the round-count cases.
- `python3 -m unittest discover -s scripts/production -p 'test_dailies_input.py'` green, with the `fixes` cases.
- `basedpyright scripts/delegate/phase_table.py scripts/delegate/test_phase_table.py scripts/production/dailies_input.py scripts/production/test_dailies_input.py` reports `0 errors, 0 warnings`.
- Observable: a dailies input built from a judgment with no `eta.fixes`, for a record-backed unit that stated an ETA and then started two repair rounds, carries `eta.fixes` `2`, and the rendered report reads `2 fix rounds added`.

## Source

From `docs/plans/build-followups-phase-tables-next.md`; the user chose both items (2026-10-09), and added for the archive: keep the project phases in the hanadocs obsidian repo.

- [ ] **The dailies count a phase's repair rounds from the run's records**
  - Target: `scripts/production/dailies_input.py` (`eta.fixes`), fed by `scripts/delegate/phase_table.py`
  - Why needed: after the dailies phase, `eta.fixes` is the one ETA number the showrunner still types; the recorder already holds each repair pass and the first stated ETA's time.
  - Completion condition: a dailies input built with no `eta.fixes` in the judgment carries the count of repair rounds started after the phase's first stated ETA.
  - Revealed by: Phase 1

- [ ] **A wrapped production's notes are archived**
  - Target: `scripts/delegate/phase_table.py` (an archive command for one production's notes), run by `scripts/production/production_lifecycle.py` (`wrap`)
  - Why needed: nothing moves a finished production's notes out of the way, and a later unit that takes the same session name under another production is refused at every report because the target is another unit's note. The user wants the phases kept in the vault rather than deleted (2026-10-09).
  - Completion condition: after a wrap, each generated note of that production sits under an archive folder named for it (for example `showrunners/<showrunner>/archive/<production>/<session>.md`), notes written by hand are untouched, and a unit of another production with the same session name gets a fresh note at its first report.
  - Revealed by: Phase 2
