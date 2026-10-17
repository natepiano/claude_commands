# unit-board

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** One recorded ETA source with chained predictions, then a Claude Code mod that draws the production board, a unit's detail and the accounts table in a pane opened only on demand.

> **Production: build-followups** — unit `unit-board-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-16 16:31 EDT

Build the approved unit board mod proposal ~/.local/state/plan-backlog/2026-10-16-unit-report-mod.md, with the user's change: the board opens and closes only on demand, and nothing shows by itself (no automatic pane, no automatic band). The user's words: 'i feel like the /unit:report with all of its tables could probably use the claude code mod ability to create something much more visually appealing to show the phase results, etas, and the other data that it shows'; 'i would prefer that it only opens and closes on demand for now'. Keep it current with no new process and no work while the board is closed. Do the ETA data fixes (proposal Phase 1) first.

Add-on, 2026-10-16 (the showrunner, approved by the user): an accounts view in the same mod. Same rules as the board: opened and closed only on demand by its own command, nothing shown by itself. It draws what `accounts.py` reports (the units/showrunners line and the accounts table, problems under it), straight to the user's screen, so login addresses never pass through a model reply. No work while closed; while open, refresh from data that already exists rather than spawning `accounts.py` on a timer; one run on open is fine.

## Delegation Context

- **Project:** `~/.claude` (claude_commands) — the recorder and phase tables behind `/unit:direct`, and a new Claude Code mod `mods/unit-board/`.
- **Project started:** 2026-10-16T20:40:19.366+00:00
- **Worktree:** `/home/natepiano/worktrees/claude-build-followups-unit-board`, branch `build-followups-unit-board`. Every path below is relative to it.
- **Stack:** Python 3 (stdlib only, `unittest`, basedpyright strict); TypeScript Claude Code mod API (early access, Claude Code 2.1.296), JSX with `h` as the factory, no DOM, no Node — everything outside the module goes through `$`.
- **Layout:**
  - `scripts/delegate/` — `progress_history.py` (recorder), `phase_table.py` (phase table, chain and vault note), `plan_history.py` (plan and run readers both share), `findings.py`, new `board_json.py`; tests beside them.
  - `scripts/production/` — `production_units.py`, `unit_run.py`, `unit_lookup.py`, `units_table.py`, `showrunners.py` (read only by this plan's code).
  - `scripts/accounts/` — `accounts.py` and its notes readers (the accounts view's data).
  - `mods/unit-board/` — new mod. `mods/postmaster/` is the house example of a mod: copy its `tsconfig.json`, `.claude-plugin/plugin.json` shape and test style (`tests/kit.test.ts`, `tests/register.test.ts`).
  - Reference only, outside the repo: the prototype `~/.local/state/plan-backlog/2026-10-16-unit-report-mod/prototype/unit-board/` (`hooks/register.tsx`, `types/index.d.ts`, `data/board.json`), the mockups `mockup-A-board.txt` and `mockup-B-unit.txt` beside it (open the `.ansi` twins with `less -R`), and the engine API `prototype/source/claude-code.d.ts`, `reference.md`, `SKILL.md`, `examples/pane.tsx`.
- **Key files:**
  - `scripts/delegate/progress_history.py` — subcommands at `_build_parser` 4774–4945; `eta` subcommand 4891, `_eta` 4506–4545 writes `eta_stated` (`eta_at`, `basis`, `eta_earliest_at`, `eta_latest_at`, epochs) and refuses with no active phase; `_local_eta_epoch` 402; `_event` 1183 (common fields); `_append_event` 498; `_write_state` 439 (atomic); `_refresh_phase_table` 455 (subprocess, 10 s timeout, only for plans holding `> **Production:`); `_summary_table` 4025–4068 (Project and Phase rows), `_eta_cell` 3735, `_eta_seconds` 2238 (`elapsed*(100-percent)/percent`), `_eta_band_cells` 3760, `eta_band_seconds` 3802; `_report_progress` 4260 (assembles the clocks table and round table at 4458–4481); `_print_last_recorded` 4207; `_phase_section` 4071, `_round_table` 3469 (line numbers from before Phase 2; search by name). Events in `runs/<run_id>.jsonl` under the history root, `run_id` = session dir name.
  - `scripts/delegate/plan_history.py` — `history_root()` 65 (`$PLAN_DELEGATE_HISTORY_DIR`, else `~/.local/state/plan-delegate`), `now_epoch()` 72 (tests patch it for the clock), `resolve_plan_path` 79, `plan_phases` 132 (`PlanPhase`, with `blocked_by` from `BLOCKED_BY_PATTERN` 38), `plan_runs` 156, `eta_band_seconds` 200.
  - `scripts/delegate/phase_table.py` — imports `plan_history`, never `progress_history` (`progress_history` imports `phase_table` only inside `_project_chain_eta`). `PhaseHistory` 128 (the per-phase record: `first_eta`, `moves`, `eta_trail`, `finished_at`, `delta_minutes`, `fix_rounds`, `findings`); gates `NoGate` 140, `TimedGate` 144, `UntimedGate` 152, `GateBlockedFinish` 213; `_plan_finish` 880; `_phase_history` 915 (reads only the row's own title family); `_production_gates` 980; `build_plan(plan_path)` 1023 (the chain from the open phase's stated ETA, gates applied); `build(session_dir)` 1201; `show_production(doc)` 1485; `refresh(session_dir)` 1861. `show --json` omits gates and turns `GateBlockedFinish` into null.
  - `scripts/delegate/findings.py` — `SEVERITIES = ("blocker", "minor", "nit")` 74; writes `finding_opened` (786, carries `severity`), `finding_verdict` 843 into the same run stream.
  - `scripts/production/production_units.py` — `live_unit_table(lines, slug)` 101 (rows with `Unit`, `Plan`, `Worktree`, `Branch`), `cell_value` 15.
  - `scripts/production/unit_run.py` — `local_run(root, worktree) -> RunFound(run_id, started_at) | NoMatchingRun` 49.
  - `scripts/production/unit_lookup.py` — `run_state(worktree) -> UnitState` 112 (`running`, `run-finished`, `standing-by`).
  - `scripts/production/units_table.py` — `open_step` 75 (newest open activity, else newest open pass), `task_cell` 116.
  - `scripts/production/showrunners.py` — `registered_showrunners()` 191 (`Showrunner(session, socket, slug, zone, doc)`; empty `session` = not running).
  - `scripts/lib/unit_runs.py` — `run_roots()` 30 (`/tmp/claude/unit`, then `/tmp/claude/delegate`).
  - `scripts/agents/codex_mesh.py` — `<session_dir>/mesh_roster.json` (`ROSTER_FILE` 177): per worker `status`, `role`, `thread_id`, `turn_id`; `<session_dir>/impl_worker_<slot>.json` holds `{name, slot, family, model, worker_id}`; `<session_dir>/board.log` lines `ISO [agent] kind: body`.
  - `docs/production_format.md` 225–250 — the checkpoint notice with `Phase <next> ETA: <HH:MM zone>`; 161–190 and 298 — the Gates table and `**Blocked by:** G<k>`.
  - `docs/delegate/phase_end.md` 20–21 — step 2 sends that notice.
  - `commands/unit/eta.md` 24–28 — already records with `progress_history.py eta`; `commands/unit/report.md` — the tick report (step 5, 69–115, prints the two recorder tables; step 8 the summary).
- **Test lanes:** `scripts/delegate/test_*.py` (Python phases); `mods/unit-board/tests/*.test.ts` (mod phases).
- **Build:** none for Python. Mod: `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/unit-board` (natedev has no `tsc`, `node` or `bun` on `PATH`; the engine writes `mods/unit-board/.claude-plugin/types/` only when a live session loads the folder, so a fresh checkout reports `TS2307` for `claude-code` until one does).
- **Test:** `HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo <tree> --tests-only` from the repo root (`<tree>` may be a `git archive` export in the scratchpad; there `shutdown/test_inventory`'s `test_git_checkout_observations_disable_optional_index_locks` always fails, rerun it in the worktree). Mod: `command claude plugin test mods/unit-board` (the bare `claude` is a shell alias).
- **Lint:** `basedpyright scripts/delegate scripts/accounts` — pass is its `0 errors, 0 warnings` line (it exits 3: pyrightconfig names a `.venv` no checkout has). Mod: `command claude plugin validate --strict mods/unit-board`.
- **Invariants:**
  - Nothing opens by itself (the user, 2026-10-16): no pane opened at `session.start`, no `AbovePrompt` band, no status line, no toast. A pane opens only from its slash command or a press inside an open pane, and closes from its command, its close mark or Escape.
  - No work while closed (the user, 2026-10-16): with no pane open the mod runs no timer, no `$.fs` call and no `$.process.run`; its `turn.complete` hook passes straight to `next`. While open it refreshes by `$.fs.stat` of existing files, and starts a process only when a stat shows a change (or once on open).
  - No new process (the user, 2026-10-16): no daemon, service or timer job outside the mod.
  - The mod reads the board JSON; it never computes ETAs or predictions (proposal, approved).
  - Login addresses never pass through a model reply (the showrunner's add-on, approved by the user): the accounts view's data goes from disk to the pane only; no command result text carries it.
  - Times shown to the user are in the production's zone (`USER_ZONE`, America/New_York); Python writes them with `astimezone(user_zone())` (`scripts/lib/user_zone.py`; `scripts/lib/test_user_zone` scans every script).
  - `.gitignore` is `*` plus allow entries; `!mods/` and `!mods/**` already allow the mod folder, and `mods/**/.claude-plugin/types/` keeps engine-generated types out.

## Phases

### Phase 1 — Every stated ETA is recorded  · status: done
**Depends on:** none

#### As-built

- `progress_history.py eta` takes `--phase-id <id>`; every `eta_stated` event carries `eta_phase_id` — the given id, else the active phase's id. No active phase and no `--phase-id` refuses.
- `--time`, `--earliest` and `--latest` take `HH:MM` (today, user zone) or `YYYY-MM-DDTHH:MM`; past times are refused. `basis` values: `checkpoint` (the checkpoint notice), `eta-command` (`/unit:eta`, `/unit:eta_breakdown`), `stated` (any other message).
- `phase_table._instance_eta_events(runs, instance)` selects an instance's `eta_stated` events by `eta_phase_id` (falling back to `phase_id` for older events) within the same run, after the previous start or finish of that phase id and before the next start of it, so an ETA recorded before its phase starts counts for that phase. `_latest_stated_eta(runs, instance)` builds on it.
- `commands/unit/direct.md` `<ProgressContract>` records every ETA the unit director states before stating it (`--phase-id` for a phase other than the active one) and states the recorder's printed time. `docs/delegate/phase_end.md` step 2 and `docs/production_format.md` item 3 record the next-phase ETA with `--basis checkpoint` before the notice. `commands/unit/eta.md` and `commands/unit/eta_breakdown.md` use `--basis eta-command`; `eta_breakdown` records on every answer.

**Files:**
- `scripts/delegate/progress_history.py` — `eta` subcommand with `--phase-id` and `eta_phase_id`; short-time expansion in `_local_eta_epoch`.
- `scripts/delegate/phase_table.py` — `_instance_eta_events`, `_latest_stated_eta`.
- `scripts/delegate/test_progress_history.py`, `scripts/delegate/test_phase_table.py` — target, default, refusal, pre-start, legacy and short-time cases.
- `docs/production_format.md`, `docs/delegate/phase_end.md`, `commands/unit/direct.md`, `commands/unit/eta.md`, `commands/unit/eta_breakdown.md` — the recording rule.

**Binds later work:** a phase record's `first_eta` and `eta_trail` come from `_instance_eta_events`, so a checkpoint-notice ETA counts as first; `moves` counts only time changes, since unchanged restatements are recorded too. Board display of `eta_phase_id` ETAs belongs to "Board data" and "The board mod".

**Gotchas:** readers place an ETA by `eta_phase_id`, never `phase_id` (which names the phase active when it was recorded). `HH:MM` means today only; a later-day ETA needs the full date.

### Phase 2 — Predictions chain from the stated ETA  · status: done
**Depends on:** 1

#### As-built
- `scripts/delegate/plan_history.py` holds the plan readers shared by `phase_table` and the recorder: `plan_runs`, `plan_phases`, `PlanPhase`, `history_root`, `now_epoch`, `resolve_plan_path`, `eta_band_seconds`, `percent_spread`, `BLOCKED_BY_PATTERN`, `STATE_FILENAME`, `EtaProjectionUnavailable`.
- Open phase end: the latest future `eta_stated`; a passed one is advanced to now for scheduling (a phase never ends in the past); with none, the `_reported_eta` projection. `build_plan` lays todo phases back to back from that end, `typical` long with `gap` between.
- Gates: `_production_gates` reads the production doc's `## Gates` table (`Gate | Waiting | Waits on | Clears when`) into `NoGate`, `TimedGate(gate, until, waits_on)` or `UntimedGate(gate, waits_on)` per `**Blocked by:** G<k>`. `Clears when` starting `cleared` is open; a `YYYY-MM-DD HH:MM` (production zone, first match) holds the phase's start to that time and prints `gated until <ddd DD Mon HH:MM>`; no time leaves that phase and every later one unpredicted, printing `gated by G<k>: <Waits on>`. Outside a production `Blocked by` is ignored.
- Plan finish is a time or `GateBlockedFinish(gate, waits_on)`.
- `PhaseHistory` (`first_eta`, `moves`, `eta_trail`, `finished_at`, `delta_minutes`, `fix_rounds`, `findings`) is the base of the done/open/todo row types. `first_eta`/`eta_trail` build on `_instance_eta_events`; `moves` counts only time changes; `eta_trail` keeps the real promises even when the schedule is advanced to now.
- History and timing read only the row's own title family: a retired task's attempts under a reused phase number are excluded.
- `progress_history._summary_table` takes the state; Project ETA is the chain's finish, low/high shift it by the open phase's band; under an untimed gate ETA reads `after G<k>` and low/high read `—`. Percent and elapsed are unchanged.
- `phase_table show --json` exposes the history fields, omits gates, and renders `GateBlockedFinish` as null.

**Files:** `scripts/delegate/plan_history.py`, `phase_table.py`, `progress_history.py`, `test_phase_table.py`, `test_progress_history.py`; `scripts/production/units_table.py`, `proposals_table.py` and their tests import `plan_history`; launcher test fixtures copy `plan_history.py`; `docs/as-built/phase-tables.md`, `docs/as-built/showrunner-automation.md`.

**Binds later work:** "Board data" builds per-todo gate and per-unit finish from `build_plan` (not `show --json`) and reads `PhaseHistory`; "The board mod" and "Shorter terminal report" consume them; all import plan readers from `plan_history`.

**Gotchas:**
- `phase_table` imports `plan_history`, never `progress_history`; `progress_history` imports `phase_table` only inside `_project_chain_eta`. basedpyright's import-cycle check is file-level with no line anchor and file-level ignores are forbidden, so any cycle must be removed, not suppressed.
- Tests patch `plan_history.now_epoch` for the clock.

### Phase 3 — Board data  · status: done

#### As-built

- `~/.claude/scripts/lib/py ~/.claude/scripts/delegate/board_json.py (--cwd <path> | --production <slug>)` writes each resolved production's board atomically (temp file in the same dir, then `os.replace`) to `${UNIT_BOARD_DIR:-${XDG_STATE_HOME:-~/.local/state}/unit-board}/<slug>.json` and prints one line `{"files": [<paths>], "focus": <unit key or null>}`; nothing resolved exits 0 with `"files": []`. It logs nothing and reads only the production's units' files.
- Resolve: `--cwd` inside a unit worktree (`showrunners.registered_showrunners()` + `production_units.live_unit_table`) gives that production with `focus` = the unit; inside a showrunner checkout, its production; anywhere else, every production whose showrunner is running, `focus` null.
- Top level: `production`, `as_of`, `zone`, `utc_offset_minutes` (at `as_of`), `units`, `sources`. Every time is `{"at": <epoch s>, "text": "HH:MM"}` in the production zone, `ddd DD Mon HH:MM` when not today.
- Per unit: `key` (Unit cell less `-unit`), `plan`, `state`, `phase {id, of, title}` or null, `phases` (each with `status` `done|open|todo|gated`, `title`, record `took`/`fix_done`/`fix_running`/`findings {blocker, minor, nit}`/`eta {first, trail, finished, delta_minutes}`, todo `start`/`finish`, and `gate` `{kind: none}` | `{kind: timed, gate, until, waits_on}` | `{kind: untimed, gate, waits_on}`; a non-`none` gate makes the status `gated`), `finish` (`predicted` | `finished` with `at`, `after_gate` with `gate`/`waits_on`, or `unknown`), `stage` (`Build`/`Tests`/`Review`/`Repair round <n>` from the open pass, else the open activity label, else `Run done`/`Standing by`/`Waiting on G<k>`), `eta {at, first, trail, moved, delta_minutes, schedule_end, fix_done, fix_running}` or null, `summary` (newest `progress_reported` text), `steps` (`[state, what, started, took_seconds]`), `workers` (`slot`, `role`, `state`, `model`, `doing` ≤ 80 chars from `board.log`).
- `phase_table` exposes `OpenPhase.instance_id`, `OpenPhase.schedule_end` (the chain's open end for a stated, projected or duration-only estimate) and `PhaseHistory.fix_open_passes`; `fix_running` is a 0/1 property and `fix_rounds` stays the sum of done and open fix passes. Tests pin resolve and focus, every stage, gate and `finish` kind, the promise/schedule split, the fix split, existing-only `sources`, the atomic write, and every output key against `board.d.ts`.

**Files:**
- `scripts/delegate/board_json.py` — the board data generator.
- `scripts/delegate/test_board_json.py` — its tests.
- `mods/unit-board/types/board.d.ts` — the JSON contract, field for field.
- `scripts/delegate/phase_table.py`, `scripts/delegate/test_phase_table.py` — fix split, open instance id, schedule end.

**Binds later work:** the CLI and its `{"files", "focus"}` output line; the `<slug>.json` location under `UNIT_BOARD_DIR`; `board.d.ts` as the contract the output is tested against; `eta.at`/`first`/`trail` are stated promises only, while `schedule_end` is the chain's end (now once a promise has passed) and never a trail entry; `fix_done` counts finished fix passes and `fix_running` is 0 or 1; `state` comes from the unit's own newest run (standing-by, running, run-finished); `steps` and `stage` follow `build_plan`'s selected open instance; `OpenPhase.instance_id` and `OpenPhase.schedule_end`; `fix_open_passes` with `fix_rounds` as the sum. `sources` lists existing paths only: each unit's run `.jsonl`, `mesh_roster.json`, `board.log`, every `impl_worker_<slot>.json`, the plan doc, every `plan_history.plan_runs` `.jsonl`, the production doc, plus each session dir and the history `runs/` dir so a run that starts later moves a watched mtime. The recorder does not write the board file.

**Gotchas:** `build_plan` can select an older resumed phase as open, so the newest `phase_started` is the wrong instance for `steps`/`stage`. `phase_table show --json` drops gates and turns `GateBlockedFinish` into null; the board builds from `build_plan`'s record instead.

**Ruled out:** `unit_lookup.run_state` for `state`, which reads every history run file per unit.

### Phase 4 — The board mod  · status: done

#### As-built

- `/unit-board [<unit>|close]` runs `$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/delegate/board_json.py --cwd <cwd>` once and opens pane `unit-board` (`Units`): the unit view for `<unit>` or `focus`, else the board view; `open`/`board` buttons switch views. `session.start` only registers the command and, if `$.ui.panes()` shows the pane open (reload), restarts its refresh; nothing shows by itself.
- The generator prints `BoardResult { files; focus; watch }`; `watch` is the existing notifier registry dir, so an open pane finds a production started later.
- Pane state (`unit-board.panes`, keyed by pane id): `loading` | `board {data, now}` | `unit {data, production, unit, now}` | `failed {reason}`; closed = no entry. A module-level `OpenPane {timer, cwd, busy, paths, selection}` per pane (`Selection = {kind:'board'} | {kind:'unit'; production; unit}`) keeps paths and selection across a failed refresh.
- Refresh only while open: a 10 s `$.clock.every` tick and `turn.complete` call `refreshIfChanged(files, paths, generator, set, isOpen)` with `RefreshPaths = {kind:'board'; board; sources} | {kind:'discovery'; sources; loadedAt}` (sources newer than the board file, or than `loadedAt`, run the generator once). `isOpen` is checked before each stat, the generator and the set. An unchanged tick only moves the ruler's `now`. `ui.close` (any origin) cancels the timer and drops the state; closed, `turn.complete` calls `next` at once.
- `loadBoard(calls, cwd, isOpen, watch)` returns `{files, boards, focus, watch, loadedAt}`; the registry comes from `NOTIFIER_STATE_DIR` (default `$HOME/.local/state/notifier`).
- Views draw the `as_of` snapshot and never recompute an ETA; the ETA mark is `eta.at`, never `schedule_end`. The strip shows counts above 30 phases; the workers line groups by role and state. Theme-key colours only; no band, status line or toast.

**Files:**
- `mods/unit-board/hooks/register.tsx` — all direct engine calls; `data.ts` — refresh and load; `geometry.ts` — pure strip and ruler; `board.tsx`, `unit.tsx` — views.
- `mods/unit-board/types/board.d.ts`, `types/index.d.ts` — generator contract; pane state.
- `mods/unit-board/tests/` — kit, geometry and register tests; `mods/.claude-plugin/marketplace.json` — local marketplace.

**Binds later work:** "Accounts view" reuses `refreshIfChanged` with its own timer and state, an `isOpen` guard and tagged paths (`discovery` needs no output file); its engine calls stay in its register file; there is no `$.ui.list()`.

**Gotchas:**
- The mod runs the generator installed under `$HOME/.claude`, not the worktree's, so a contract change cannot be live-checked before promotion; the board's live and design checks wait for promotion.
- A failed stat counts as a change: a `failed` pane retries each tick while a source stays newer than the board file, and recovers on the next good run.
- The user installs per machine (`/plugin marketplace add ~/.claude/mods`, `/plugin install unit-board`); `settings.json` is never edited.

**Ruled out:** a scratch HOME for the live check (refused as credential exploration); a watcher process or timer outside the mod.

### Phase 5 — Shorter terminal report  · status: todo
**Depends on:** 4

#### Work Order

**Goal:** `/unit:report` and its notifier ticks print a short text report — phase and stage, one ETA line, the workers, the summary — and the two recorder tables only on `/unit:report tables`.

**Spec:**
- The ETA line, printed by `progress_history.py progress` and by `_print_last_recorded`: `ETA 18:30 (first 17:15, +1:15, moved 2×, 1 fix round + 1 running)`; on its first ETA `ETA 16:10 (on its first ETA, 1 fix round)`; gated `gated until Wed 22 Oct 11:47` or `gated by G3: <Waits on>`. From the phase record and chain of Phase "Predictions chain from the stated ETA" and the `fix_done`/`fix_running` split of Phase "Board data": `ETA` is the latest promise, never the schedule end an overdue phase is advanced to; an overdue phase reads `ETA 18:30 (passed, first 17:15, …)`.
- The clocks table and round table (`_summary_table`, `_phase_section`) print only with a new `progress --tables` flag; `commands/unit/report.md` passes it only for `/unit:report tables`.
- **Report inputs stay.** `_phase_section` also supplies the worker narration and stage the report's worker table is built from. Without `--tables`, `progress` still prints the stage line and the workers (slot, role, state, model, what each is doing), from the same sources as today, so `commands/unit/report.md` can build its worker table with the tables hidden.
- `commands/unit/report.md`: the report's sections become phase and stage, the ETA line, the worker table, the summary sentences, and one line naming `/unit-board` as the place to see the board.
- The vault note keeps the full phase list.

**Files:**
- `scripts/delegate/progress_history.py` — the ETA line and `progress --tables`.
- `scripts/delegate/test_progress_history.py` — the line forms and the tables flag.
- `commands/unit/report.md` — tables only on request; names `/unit-board`.

**Workers:** 1 writer + 1 tester
- `impl` — `progress_history.py`, `commands/unit/report.md`; hub: `scripts/delegate/progress_history.py`.
- `test` — `progress` prints the ETA line forms above, the stage and worker lines, and no tables; `progress --tables` prints both tables as before; between windows (`_print_last_recorded`) the same short form prints; an activity still open after its phase finished still shows as the stage; an overdue unchanged promise prints `passed`, never a new time.

**Constraints from prior phases:** the phase record (`first_eta`, `moves`, `eta_trail`, `delta_minutes`, `fix_done`, `fix_running`) and the gate text come from `phase_table` (`build_plan`, gates `NoGate`/`TimedGate`/`UntimedGate`); plan readers from `plan_history`, and tests patch `plan_history.now_epoch`. An overdue open phase's `StatedEta.time` is the schedule end, not a promise; `OpenPhase.schedule_end` holds the chain's end for the open phase, also from a duration estimate, and `OpenPhase.instance_id` names the open instance. `PhaseHistory.fix_running` is 0 or 1, `fix_open_passes` counts open fix passes, and `fix_rounds` is `fix_done + fix_open_passes`. The board command is `/unit-board`.

**Acceptance gate:** the Test and Lint lines of Delegation Context green.

### Phase 6 — Accounts view  · status: todo
**Depends on:** 4

#### Work Order

**Goal:** `/accounts-board` opens a pane showing the units/showrunners line, the accounts table with each login's address, and the problems under it, drawn from disk straight to the screen, current while open, with nothing running while closed.

**Spec:**
- **Data source.** two-accounts-unit's stored status with logins is promoted (49fde9b3; the showrunner, 2026-10-16): `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py status --json --stored --with-login`. It prints `{"accounts": [{label, tool, roles[], weekly_left_percent, resets_at, credits, use_credits, read_at, login}], "problems": [...]}`. `--stored` reads stored readings only — no live read, no subprocess, no `ssh`; `--with-login` needs `--json`. Never parse the text table. Copy the exact field types into `mods/unit-board/types/accounts.d.ts` from the code on the merge branch. Every run this phase makes, the open run included, is exactly that command. This phase does not edit `accounts.py`.
- **The mod never opens a credential file** (the showrunner, 2026-10-16: those files are read for nothing beyond what they must be): no read of `~/.claude<N>/.claude.json`, any `auth.json` or any `id_token`, not even to decode the email. Login addresses come only from the `login` field of `--with-login`, and are only drawn: never in a command result, `$.ui.log`, a toast or a message.
- **Mod** (`hooks/accounts.tsx`, wiring in `register.tsx`): `session.start` also registers `/accounts-board`, and restarts its refresh when `$.ui.panes()` shows the pane open. `/accounts-board` runs the command once, opens pane `accounts-board` (title `Accounts`), and draws: the units/showrunners line built from the `roles` fields; the table (Label, Login, Roles (comma-joined), Weekly left, Weekly resets, On credits, Credits left, Read; weekly left under 10 % `error`, under 25 % `warning`); the problems under it. `/accounts-board close` closes it. The command result text is `Accounts opened.` only.
- **Refresh while open only.** `$.clock.every(10_000)` stats `~/.local/state/agent-notes/usage.json`, `~/.local/state/accounts/groups.json` and `~/.claude/config/accounts.json`; on a newer mtime run the same command once and redraw. `usage.json` moves about every 2 min (the `agent-sessions-snapshot` timer), so an open pane runs one stored read per move and none between. `ui.close` cancels the timer; `turn.complete` does nothing for this pane. Closed: no timer, stat or process. Reuse the board's stat-and-run helper in `hooks/data.ts`, with this pane's own state entry (`{kind: "loading"}`, `{kind: "accounts", data}`, `{kind: "failed", reason}`) and its own timer: either pane opens and closes without touching the other's. Any change the helper or the shared pane state needs is this phase's writer's.
- **Tests** (`tests/accounts.test.ts`): `/accounts-board` opens and draws a fixture with logins and rows holding several roles; the open run and each refresh run are called with exactly `[py, accounts.py, "status", "--json", "--stored", "--with-login"]`; no `$.fs` call names a `.claude.json`, `auth.json` or `id_token` path; a changed `usage.json` mtime runs the stored read once and an unchanged one runs nothing; closed → nothing over a fake-clock minute; the command result holds no address; with both panes open, closing either stops only its own timer, stats and runs while the other keeps refreshing.
- **Live check:** in the throwaway session, `/accounts-board` draws the table; touching `usage.json` redraws within 10 s; with `/unit-board` also open, closing the accounts pane stops its stats while the board keeps refreshing, and the reverse; closing both stops every stat. Capture the pane to a scratchpad file and confirm it by headings and row count only, never pasting rows into a reply or message.

**Files:**
- `mods/unit-board/hooks/accounts.tsx` — the accounts view.
- `mods/unit-board/hooks/register.tsx` — the wiring for `/accounts-board`.
- `mods/unit-board/hooks/data.ts`, `mods/unit-board/types/index.d.ts` — only what the second pane needs from the shared helper and pane state.
- `mods/unit-board/types/accounts.d.ts` — the stored-status JSON shape.
- `mods/unit-board/tests/accounts.test.ts` — the view's tests.

**Workers:** 1 writer + 1 tester
- `impl` — `hooks/accounts.tsx`, `types/accounts.d.ts`, `hooks/register.tsx`, and any shared helper or pane-state change in `hooks/data.ts` and `types/index.d.ts`; hub: `mods/unit-board/hooks/register.tsx`.
- `test` — `tests/accounts.test.ts` from the Spec and a fixture in the merged JSON's shape, and any change the shared helper needs in the board's tests.

**Constraints from prior phases:** the mod's layout, `register.tsx` wiring-only rule, the open/close and closed-quiet pattern, the 10 s stat refresh, the per-pane tagged state and timer, the `hooks/data.ts` stat-and-run helper and the test kit usage come from Phase "The board mod".

**Constraints from "The board mod" as built:** `refreshIfChanged(files, paths, generator, set, isOpen)` takes an `isOpen` guard and tagged paths: `{kind: "board", board, sources}` or `{kind: "discovery", sources, loadedAt}`; the accounts pane has no output file, so its inputs are `discovery` paths compared against `loadedAt`. A failed stat counts as a change, so watch only files that exist, plus their parent directory as a `discovery` path: initial absence stays quiet, creation refreshes once, and deletion refreshes without regenerating on every tick (tests for all three). Refresh inputs also include `~/.local/state/accounts/role_map_cache.json` (stored roles on machines other than natedev), with a test that changing it redraws the roles. A pane's watched paths and selection live in its module-level entry beside its timer; a failed load or refresh shows its reason and recovers on the next watched change, and closing during a pending run stops every later read and state write, with the other pane unaffected (tests for a failed first load, a failed refresh, and a close while the command runs). Use `$.ui.panes()` (there is no `$.ui.list()`). Direct engine calls stay in `register.tsx`; imported helpers receive narrow services, and views receive resolved elements. `types/accounts.d.ts` keeps the exact wire shape, nullable fields and `credits: number | "unlimited" | null` included; convert it into named display states (for example a reading that is unavailable; credits finite, unlimited or unavailable), with a test for each. The mod always runs the generator under `$HOME/.claude`, so the live check runs only after the matching generator is promoted to main; it also carries the board's deferred live check (trunk → A, unit worktree → B, closed-quiet captures).

**Acceptance gate:** `tsc`, `command claude plugin validate --strict mods/unit-board` and `command claude plugin test mods/unit-board` exit 0; the live check above, after promotion, both panes open and each closed in turn.
