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
  - `scripts/direct/` — `progress_history.py` (recorder), `phase_table.py` (phase table, chain and vault note), `plan_history.py` (plan and run readers both share), `findings.py`, new `board_json.py`; tests beside them.
  - `scripts/production/` — `production_units.py`, `unit_run.py`, `unit_lookup.py`, `units_table.py`, `showrunners.py` (read only by this plan's code).
  - `scripts/accounts/` — `accounts.py` and its notes readers (the accounts view's data).
  - `mods/unit-board/` — new mod. `mods/postmaster/` is the house example of a mod: copy its `tsconfig.json`, `.claude-plugin/plugin.json` shape and test style (`tests/kit.test.ts`, `tests/register.test.ts`).
  - Reference only, outside the repo: the prototype `~/.local/state/plan-backlog/2026-10-16-unit-report-mod/prototype/unit-board/` (`hooks/register.tsx`, `types/index.d.ts`, `data/board.json`), the mockups `mockup-A-board.txt` and `mockup-B-unit.txt` beside it (open the `.ansi` twins with `less -R`), and the engine API `prototype/source/claude-code.d.ts`, `reference.md`, `SKILL.md`, `examples/pane.tsx`.
- **Key files:**
  - `scripts/direct/progress_history.py` — subcommands at `_build_parser` 4774–4945; `eta` subcommand 4891, `_eta` 4506–4545 writes `eta_stated` (`eta_at`, `basis`, `eta_earliest_at`, `eta_latest_at`, epochs) and refuses with no active phase; `_local_eta_epoch` 402; `_event` 1183 (common fields); `_append_event` 498; `_write_state` 439 (atomic); `_refresh_phase_table` 455 (subprocess, 10 s timeout, only for plans holding `> **Production:`); `_summary_table` 4025–4068 (Project and Phase rows), `_eta_cell` 3735, `_eta_seconds` 2238 (`elapsed*(100-percent)/percent`), `_eta_band_cells` 3760, `eta_band_seconds` 3802; `_report_progress` 4260 (assembles the clocks table and round table at 4458–4481); `_print_last_recorded` 4207; `_phase_section` 4071, `_round_table` 3469 (line numbers from before Phase 2; search by name). Events in `runs/<run_id>.jsonl` under the history root, `run_id` = session dir name.
  - `scripts/direct/plan_history.py` — `history_root()` (returns `unit_runs.history_root()`), `now_epoch()` 72 (tests patch it for the clock), `resolve_plan_path` 79, `plan_phases` 132 (`PlanPhase`, with `blocked_by` from `BLOCKED_BY_PATTERN` 38), `plan_runs` 156, `eta_band_seconds` 200.
  - `scripts/direct/phase_table.py` — imports `plan_history`, never `progress_history` (`progress_history` imports `phase_table` only inside `_project_chain_eta`). `PhaseHistory` 128 (the per-phase record: `first_eta`, `moves`, `eta_trail`, `finished_at`, `delta_minutes`, `fix_rounds`, `findings`); gates `NoGate` 140, `TimedGate` 144, `UntimedGate` 152, `GateBlockedFinish` 213; `_plan_finish` 880; `_phase_history` 915 (reads only the row's own title family); `_production_gates` 980; `build_plan(plan_path)` 1023 (the chain from the open phase's stated ETA, gates applied); `build(session_dir)` 1201; `show_production(doc)` 1485; `refresh(session_dir)` 1861. `show --json` omits gates and turns `GateBlockedFinish` into null.
  - `scripts/direct/findings.py` — `SEVERITIES = ("blocker", "minor", "nit")` 74; writes `finding_opened` (786, carries `severity`), `finding_verdict` 843 into the same run stream.
  - `scripts/production/production_units.py` — `live_unit_table(lines, slug)` 101 (rows with `Unit`, `Plan`, `Worktree`, `Branch`), `cell_value` 15.
  - `scripts/production/unit_run.py` — `local_run(root, worktree) -> RunFound(run_id, started_at) | NoMatchingRun` 49.
  - `scripts/production/unit_lookup.py` — `run_state(worktree) -> UnitState` 112 (`running`, `run-finished`, `standing-by`).
  - `scripts/production/units_table.py` — `open_step` 75 (newest open activity, else newest open pass), `task_cell` 116.
  - `scripts/production/showrunners.py` — `registered_showrunners()` 191 (`Showrunner(session, socket, slug, zone, doc)`; empty `session` = not running).
  - `scripts/lib/unit_runs.py` — `run_roots()` 30 (`/tmp/claude/unit`, then `/tmp/claude/delegate`).
  - `scripts/agents/codex_mesh.py` — `<session_dir>/mesh_roster.json` (`ROSTER_FILE` 177): per worker `status`, `role`, `thread_id`, `turn_id`; `<session_dir>/impl_worker_<slot>.json` holds `{name, slot, family, model, worker_id}`; `<session_dir>/board.log` lines `ISO [agent] kind: body`.
  - `docs/production_format.md` 225–250 — the checkpoint notice with `Phase <next> ETA: <HH:MM zone>`; 161–190 and 298 — the Gates table and `**Blocked by:** G<k>`.
  - `docs/direct/phase_end.md` 20–21 — step 2 sends that notice.
  - `commands/unit/eta.md` 24–28 — already records with `progress_history.py eta`; `commands/unit/report.md` — the tick report (step 5, 69–115, prints the two recorder tables; step 8 the summary).
- **Test lanes:** `scripts/direct/test_*.py` (Python phases); `mods/unit-board/tests/*.test.ts` (mod phases).
- **Build:** none for Python. Mod: `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/unit-board` (natedev has no `tsc`, `node` or `bun` on `PATH`; the engine writes `mods/unit-board/.claude-plugin/types/` only when a live session loads the folder, so a fresh checkout reports `TS2307` for `claude-code` until one does).
- **Test:** `HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo <tree> --tests-only` from the repo root (`<tree>` may be a `git archive` export in the scratchpad; there `shutdown/test_inventory`'s `test_git_checkout_observations_disable_optional_index_locks` always fails, rerun it in the worktree). Mod: `command claude plugin test mods/unit-board` (the bare `claude` is a shell alias).
- **Lint:** `basedpyright scripts/direct scripts/accounts` — pass is its `0 errors, 0 warnings` line (it exits 3: pyrightconfig names a `.venv` no checkout has). Mod: `command claude plugin validate --strict mods/unit-board`.
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

### Phase 5 — Shorter terminal report  · status: done

#### As-built

- `progress` (and `_print_last_recorded` between windows) prints the short report by default: the phase heading, one ETA line from `_phase_eta_line`, the stage line and one `- **Agent N**` line per worker (slot, role, elapsed, state, model, last board line). The ETA line forms are `ETA HH:MM (first HH:MM, +H:MM, moved N×, …)`, `ETA HH:MM (on its first ETA, …)`, a leading `passed` when an open phase is overdue, fix rounds as `N fix rounds`, `+ 1 running` or `1 fix round running`, `gated until Wed 22 Oct 11:47`, `gated by <gate>: <Waits on>`, and `ETA unavailable` when no plan, state, row or promise exists. A promise on another local day shows weekday and date.
- `ETA` is the latest promise in `eta_trail`, never the schedule end an overdue phase is advanced to, and the line reads only the reported phase's own row and gate.
- Worker lines come from `_current_worker_windows` / `_current_worker_cells`: per slot, the live window, else the most recent finished one, independent of the director's own activity.
- `progress --tables` adds the clocks table (`_summary_table`) and the round table, `Earlier:` and verification gate lines; `/unit:report tables` passes it, ordinary reports and notifier ticks do not.
- The legacy (non-dual) layout keeps its old form plus one ETA line after the phase line. The vault phase note keeps the full phase list.
- `/unit:report` sections run phase and stage, ETA line, worker table, summary sentences, `See /unit-board for the board.`; `/unit:direct`'s `<DelegationResultFormat/>` closes every result and launch with this short report.

**Files:**
- `scripts/delegate/progress_history.py` — `_phase_eta_line`, `_current_worker_windows`, `_current_worker_cells`, `_phase_section(tables=…)`, `_print_last_recorded(tables=…)`, `progress --tables`.
- `scripts/delegate/test_progress_history.py` — ETA line forms, worker lines, the tables flag, between-window output.
- `commands/unit/report.md` — short report contract; tables only on `tables`; names `/unit-board`.
- `commands/unit/direct.md` — `<DelegationResultFormat/>` closes with the short report.

**Gotchas:** taking worker cells from the round table's last row blanks them once the director opens an activity; reading the next gated phase instead of the reported one gives the wrong ETA line.

### Phase 6 — Accounts view  · status: done

#### As-built

- `/accounts-board` resolves the user zone, opens pane `accounts-board` (title `Accounts`), stores `{kind: "loading"}`, runs `[~/.claude/scripts/lib/py, ~/.claude/scripts/accounts/accounts.py, "status", "--json", "--stored", "--with-login"]` once and replies `Accounts opened.`; `/accounts-board close` closes it (`Accounts closed.`). Every open and refresh run is exactly that argv; the text table is never parsed. `session.start` registers the command and, when `$.ui.panes()` shows the pane open, restarts its refresh.
- `AccountsView(elements, status, zone)` draws the units/showrunners line from `roles` (units match both `units` and `unit directors`; none → `unassigned`), the table (Label, Login, Roles comma-joined, Weekly left, Weekly resets, On credits, Credits left, Read; weekly left under 10 % `error`, under 25 % `warning`) and a Problems list. `accountDisplay(account)` turns the wire shape into `AccountReading<T>` (`available`/`unavailable`) and `CreditBalance` (`finite`/`unlimited`/`unavailable`); dates format in the resolved zone.
- `resolveUserZone(calls, isActive): Promise<ZoneLookup>` mirrors `scripts/lib/user_zone.py`: `USER_ZONE` env, then the `zone=` line of `USER_ZONE_CONF` or `~/.claude/config/user_zone.conf`, then `America/New_York`, validated through `Intl.DateTimeFormat`; returns `{kind: "cancelled"}` when the pane lifecycle moves on.
- `loadAccounts(calls, isOpen, watch)` watches `usage.json`, `groups.json`, `~/.claude/config/accounts.json` and `role_map_cache.json` as one `discovery` path against `loadedAt`: an existing input is watched by itself, a missing one by its nearest existing parent. A 10 s `$.clock.every` tick calls the shared `refreshIfChanged` with a `busy` guard; a newer mtime reruns the stored read once and redraws. Pane state `AccountsPaneState<Status>` (`loading` | `accounts {data, zone}` | `failed {reason}`) lives under state key `accountsPanes`, apart from the board's `panes`, with its own timer, so either pane opens and closes without touching the other.
- Invariants: pane closed = no timer, no stat, no process (`ui.close` bumps the lifecycle counter and cancels the timer; `turn.complete` does nothing for this pane). The mod never opens `.claude.json`, `auth.json` or an `id_token`; logins come only from the `login` field and are only drawn, never in a command reply, log, toast or message. A failed run surfaces a fixed reason, never process output.

**Files:**
- `mods/unit-board/hooks/accounts.tsx` — `AccountsView`, `accountDisplay` and the display-state types.
- `mods/unit-board/hooks/data.ts` — `refreshIfChanged`, `loadAccounts`, `resolveUserZone` and their narrow service interfaces.
- `mods/unit-board/hooks/register.tsx` — `/accounts-board` command, open/close lifecycle, timer and render wiring.
- `mods/unit-board/types/accounts.d.ts` — `StoredAccount` / `StoredAccountsStatus` wire shape (`credits: number | "unlimited" | null`, nullable readings, optional `login`).
- `mods/unit-board/types/index.d.ts` — `AccountsPaneState` and the `accountsPanes` state key.
- `mods/unit-board/tests/accounts.test.ts` — argv, no credential paths, no address in replies, per-input mtime refresh, missing-input discovery, closed quiet, independent panes, failure recovery, close during a pending run, zone order, display states.

**Gotchas:** the mod always runs the generator under `$HOME/.claude`, so the live check of the pane with real data runs after promotion to main, together with the board's deferred live check. A close during a pending `session.start` load must not restart the timer; the lifecycle counter guards it.

### Phase 7 — The board opens at once  · status: done

#### As-built

- `unit_run.local_run` reads each run file only up to its `run_started` line, which every run file holds once, on line 1. It skips lines without `"run_started"` before parsing. Board generation for three productions fell from 35 s to 4.5 s, so the trunk board opens inside the mod's 30 s process limit.

### Phase 8 — Board layout fixes  · status: done

#### As-built

- Board (`board.tsx`):
  - Units sort running, then standing by, then run done.
  - A finished unit is one line: `Run done` plus its finish.
  - With no phase running, the stage moves to the header line.
  - The strip separator prints only with cells.
  - `workersText` skips empty parts.
  - A predicted finish shows only with a later phase predicted.
  - The workers line hides when every worker has ended.
- `board_json.py` `workers`: roster `done` reads `idle`, and `doing` drops `launcher: ` and the trailing summary or findings path, stripped.
- Unit view (`unit.tsx`):
  - Empty blocks are not drawn.
  - Worker columns pad to the widest entry.
  - An empty record reads `no phases recorded`.
- `geometry.ts`: the ruler takes the open row of the current phase id.
- `phase_table.build_run(plan, events)` builds the record from one run's events when the plan has no phases. `build_plan` and `build_run` share `_build_phase_record`, `_open_instance`, `_with_history` and `_plan_finish`.
  - Every `phase_finished` is a done row, titled from the finish event.
  - The open row is matched by instance id regardless of the title filter.

### Phase 9 — Every unit fits, and the stage is right  · status: done

#### As-built

- Board (`board.tsx`):
  - `run-finished` units with no phase fold into `Done: …`, and plain standing-by units into `Standing by: …`. Each line ends `+N more` past the width, via `doneText(keys, width, label)`.
  - A gate waiter keeps its one-line header.
  - `BoardView` takes the pane's `bodyRows`, passed in from `register.tsx`, and reserves the fold lines first.
  - When the running blocks don't fit, they go compact from the last one up: header, then stage, ETA and overdue, then the strip.
  - Then the gaps between blocks drop.
  - Then one `+N more running: …` line takes the rest.
  - Nothing draws past `bodyRows`.
  - A passed ETA or predicted finish reads `overdue Nm`.
  - Worker groups fall back to their slot.
  - Titles are cut with `…` to keep two spaces before `open`.
- `geometry.ts`: `cellWidth` counts terminal cells (wide, emoji and combining characters). `fitText`, `doneText` and `overdueText` use it.
- Unit view (`unit.tsx`):
  - With no phase running, the header shows only the stage.
  - The id, timing, fix-dot and ETA columns are each one width across the record.
  - A deleted plan path is not shown.
- `board_json.py`:
  - `stage` order: an open pass, then a dispatched batch not yet landed (`Repair round N`), then the open step, then `Phase N running`. It reads `Standing by` only with no phase open.
  - The record is built from the run's own plan (`project_plan_doc`, else `plan_doc`) when that plan differs from the Plan cell. The displayed `plan` stays the Plan cell, and is empty when that file is gone.
- Install: `mods/.claude-plugin/marketplace.json` (marketplace `nate-local`) lists `unit-board` and `postmaster`. Loading it in user sessions takes a `~/.claude/settings.json` change (`extraKnownMarketplaces` plus `enabledPlugins`), which goes to the user.

### Phase 10 — Unit view fits, and no doubled stage  · status: done

#### As-built

- Board: a unit with no phase running shows its stage once, in its header. The line under it is the strip alone, drawn only when the strip has cells.
- `board_json.workers(session, sources, mesh_prefix)`:
  - Slots drop the worktree's mesh prefix, also learned from `impl_worker_*.json` names.
  - Roster entries that become one slot merge into the entry that slot's `impl_worker_<slot>.json` names, else the last in roster order. Roster `revision` values are per-entry counters and are never compared.
- `geometry.ts`:
  - `fitText` collapses whitespace runs, newlines included, to one space before fitting.
  - `overdueText` reads `Nm`, `Nh Mm` or `Nd Mh`.
- `UnitView(elements, columns, bodyRows, …)` keeps within `bodyRows`:
  - The phase record keeps the open row and the newest rows. Older done rows fold into `+N earlier: <ids>`, and todo rows past the next three fold into `+N later`.
  - `This phase` keeps its newest steps and folds the rest into `+N earlier steps`.
  - Step labels are fitted to their column on one row.

### Phase 11 — The board is a showrunner command  · status: done

#### As-built
`/showrunner:unit_board [<unit>|close]` opens the board for the showrunner session's production and every production below it; `/unit-board` is gone. A mod command name allows only letters, digits, `_` and `-`, so `commands/showrunner/unit_board.md` gives the name and the mod answers it with a `command.run` hook that never calls `next` (no model turn); without the mod the file makes the model reply one line naming `unit-board@nate-local`. `board_json.resolve` returns the checkout's production, then `showrunners.descendants_of` in order, each only when registered; any other cwd gives no files. `focus` is gone from the output, `BoardResult`, `BoardSnapshot` and `commandSelection`; `<unit>` picks the first board holding that key. `watch` also holds `SHOWRUNNER_STATE_DIR` and each registered `<slug>` dir, so an attach or detach refreshes an open pane. With no boards the pane reads `Run /showrunner:unit_board in a showrunner session.`

**Files:** `commands/showrunner/unit_board.md`, `mods/unit-board/hooks/register.tsx`, `data.ts`, `board.tsx`, `types/board.d.ts`, `types/index.d.ts`, `scripts/delegate/board_json.py`, `commands/unit/report.md`; tests `register.test.ts`, `board.test.tsx`, `accounts.test.ts`, `kit.ts`, `test_board_json.py`.
**Gotchas:** registration creates `SHOWRUNNER_STATE_DIR/<slug>/` before the update timer, so a watched `<slug>` dir exists before any attach.

### Phase 12 — Standing plans show their ticket  · status: done

#### As-built
`board_json.unit` tags the phase: `{kind: "plan", id, of, title}` for an ordinary plan, `{kind: "ticket", id, title, merged}` when the plan holds a `> **Standing:**` line. `merged` counts the unit's `merged` tickets in the style ledger (`<state>/<main repo name>/ledger.sqlite`), opened `mode=ro` with `timeout=0`; a missing, failed or locked ledger gives 0, and the ledger file is a watched source. The board heading reads `Ticket <id> · <n> merged  <title>`, the unit view `Ticket <id> · <n> merged · <stage>`.

**Files:** `scripts/delegate/board_json.py`, `mods/unit-board/types/board.d.ts`, `mods/unit-board/hooks/board.tsx`, `mods/unit-board/hooks/unit.tsx`, tests.
**Gotchas:** never construct `style_ledger.Ledger` from the board: it opens the database with `BEGIN IMMEDIATE`. The default sqlite busy timeout (5 s) made a locked ledger stall each standing unit.

### Phase 13 — The ticket line names the ledger's ticket  · status: done

#### As-built
`unit_ticket_progress` reads the style ledger once (`mode=ro`, `timeout=0`) for the merged count and the unit's newest `assigned` or `checkpointed` ticket. A ticket phase's `id` is that ticket; with none, the title's leading `<unit>-<n>`, else the phase id. The title drops the leading id, the stage reads `Ticket <id> running`, and an empty title leaves no gap in the board heading or unit view.

**Files:** `scripts/delegate/board_json.py`, `mods/unit-board/hooks/board.tsx`, `mods/unit-board/hooks/unit.tsx`, tests.
**Gotchas:** `style_plan.py` writes `### Phase <n> — <ticket id>`, so the phase id is the bare number. The run record can lag the ledger by a ticket after refill.

### Phase 14 — The board finds its production from the session  · status: done

#### As-built
`board_json.py` takes `--session <id>`. With no `--production`, `resolve(cwd, production, session)` first takes the registered showrunner whose `doc` resolves to the path `showrunners.production_of(session)` returns, and uses the working directory only when that finds none. `loadBoard` passes `--session` from `$.session.id()` on open, on the `session.start` reopen and on every refresh (the open pane keeps it beside `cwd`). The command's fallback line names `/reload-plugins`.

**Files:** `scripts/delegate/board_json.py`, `mods/unit-board/hooks/data.ts`, `mods/unit-board/hooks/register.tsx`, `commands/showrunner/unit_board.md`, tests.
**Gotchas:** a session loads mod hooks when it starts; a session that started before a command reached main needs `/reload-plugins`.

### Phase 15 — Each account is one readable line  · status: done

#### As-built
`accountsTable(rows, zone): AccountsTable` gives seven columns (Label, Login, Roles, Left, Resets, Credits, Read), short cells (`Oct 18 14:00`, `42 in use`, `unlimited`, `—` for a missing reading) and `width` in terminal columns (`cellWidth`) plus two spaces between columns, with no padding after the last column. Rows draw in the default colour; `leftTone` gives Left `error` under 10 %, `warning` under 25 %.

**Files:** `mods/unit-board/hooks/accounts.tsx`, `mods/unit-board/tests/accounts.test.ts`.
**Gotchas:** measure and pad with `cellWidth`, never `.length`: a wide character takes two columns.

### Phase 16 — The accounts table has no Login column  · status: done

#### Work Order

**Goal:** `/accounts-board` shows no login column and no units/showrunners line, and never receives a login address.

**Spec:**

Source: the user via the showrunner, 2026-10-19 10:33: "the /accounts-board has limited space - ask it to remove the "login" column - which should free it up". The showrunner: "This also means the pane no longer shows login addresses."

1. **No Login column.** The headings are `Label`, `Roles`, `Left`, `Resets`, `Credits`, `Read`. Cell forms, colours, `width` rules and the last-column rule from phase 15 do not change.
2. **No login in the data.** The accounts run is `[~/.claude/scripts/lib/py, ~/.claude/scripts/accounts/accounts.py, "status", "--json", "--stored"]` (no `--with-login`): without the flag, `accounts.py` omits `login`. Remove `login` from `StoredAccount` (`types/accounts.d.ts`, and its doc comment) and from `AccountDisplay`/`accountDisplay`. `accounts.py` does not change.

3. **No units/showrunners line.** Source: the user via the showrunner, 2026-10-19: the top line `units: claude1, showrunners: claude3` "is unnecessary as this is spelled out clearly in the table". Remove it and the `assignments` helper; the table is the first thing in the pane, with no top margin. Problems stay under the table.

**Files:**
- `mods/unit-board/hooks/accounts.tsx` — the column, the display field and the top line.
- `mods/unit-board/hooks/data.ts` — the argv.
- `mods/unit-board/types/accounts.d.ts` — the wire type.
- `mods/unit-board/tests/accounts.test.ts` — tests.

**Workers:** 1 writer + 1 tester.
- `impl` — `mods/unit-board/hooks/accounts.tsx`, `mods/unit-board/hooks/data.ts`, `mods/unit-board/types/accounts.d.ts`.
- `test` — `mods/unit-board/tests/accounts.test.ts`, from this Spec alone: the six headings and no `Login`; the argv on open and on refresh has no `--with-login`; a status that still carries a `login` field draws no address; `width` for the realistic accounts (roles `unit directors, showrunners`, credits `unlimited in use`) is the six column widths plus 10 and at most 90; the wide-character test moves from the login to the label, so it still proves `cellWidth`. Tests that checked an address is drawn now check none is. No drawn line starts with `units:` and the heading row is the first line.

**Constraints from prior phases:** phases 6 and 15 hold: a closed pane does no work, the mod never opens a credential file, a failed run shows a fixed reason, never process output. Never capture the live accounts pane; check the layout with fixture accounts only.

**Acceptance gate:** mod tsc, mod tests, strict validate, the full merge gate.

### Phase 17 — The board is alphabetical, readable and never cut off  · status: done

#### Work Order

**Goal:** the unit board lists units by name, gives each unit at most three short lines, uses color for meaning, draws every fact in readable text, and cuts nothing off.

**Spec:**

Source: the user, 2026-10-19 15:44, with a screenshot of the board: "i want unit_board to be alphabetical - but send this screenshot to a design review team to get feedback on how to make it succinct, anbd more readable - you could be using color more intelligently i think - the dim text is hard to read - nothing should be cut off also". Three design reviewers (layout, color, what a reader needs) agreed on the items below; the unit director took the choices where they differed.

1. **Alphabetical.** Every unit of a production is in one list sorted by `key` (`localeCompare` with `sensitivity: 'base'`). No state-first sort. No `Done:` or `Standing by:` fold lines: each unit is in its place in the list.
2. **The name opens the unit.** The unit name is the `Button` (plain, name in bold). No `open` label on any row.
3. **A unit with an open phase has at most three lines** (plus a gate line, plus wrap lines):
   - Line 1: `● <key>  Phase <id>  <title>`, or `● <key>  Ticket <id> · <n> merged  <title>`. No `/<of>` after the phase id (it read `Phase 32/31` when a plan grew).
   - Line 2, left: the strip, then `<done>/<total>` (total = `phases.length`), then the stage, then ` · fix rounds <n>` when `n > 0` (` + 1 running` when one runs; the fix part in `warning` at 3 or more). Drop the stage when it is `Phase <id> running` or `Ticket <id> running`: the heading says it. Line 2, right: `ETA <time> · in 2h 16m` in the default color, or `ETA <time> · late 2h 29m` in `error`, or `no ETA` in `warning`. No `on its first ETA`, no `moved N×` on the board (the detail view keeps the ETA history).
   - Gate line, when a phase is gated: `Phase <id> waits on <gate>: <waits_on>`, timed form `Phase <id> waits on <gate> until <time>: <waits_on>`, in `warning`.
   - Line 3, left: `workers ` (in `inactive`) then counts by state, `3 running · 4 idle · 5 paused`, in the order running, idle, paused, then any other state; `running` in `suggestion`, `paused` in `warning`, the rest in the default color. No roles, no slot names (the detail view has them). Line 3, right: `run ends <time>` (predicted; in `error` with ` · late <elapsed>` when past), `run ended <time>` (finished), `run ends after <gate>` (after_gate), nothing (unknown).
4. **A unit with no open phase is one line:** `● <key>  <stage>` then ` · <done>/<total>` when it has phases, then ` · run ends <time>` / ` · run ended <time>` when known.
5. **The strip is always a bar.** One cell per phase up to 20 phases. Above 20, 20 cells: cell `i` covers phases `[floor(i*n/20), floor((i+1)*n/20))` and shows `open` if any of them is open, else `gated` if any is gated, else `todo` if any is todo, else `done`. Colors do not change: done `success`, open `suggestion`, todo `inactive`, gated `warning`.
6. **Short times** from `at` and `utc_offset_minutes` (never from `text`): the same local day as now is `18:00`; within six days before or after is `Sat 18:41`; else `24 Oct`. Elapsed and remaining use the `overdueText` units (`2h 29m`, `1d 10h`, `45m`).
7. **No ruler on the board.** It moves to the detail view only (it is already there).
8. **The dot color is the unit's health:** `error` when the ETA or the predicted run end is past; else `warning` when a phase is gated or an open phase has no ETA; else `suggestion` when the unit runs; `success` when the run finished; `inactive` otherwise.
9. **Header:** production name in bold on the left; on the right `11 units · 2 late · as of 15:44`, with `· N late` only when N > 0 (N = units with an `error` dot, the count in `error`), the rest in `inactive`.
10. **Readable text.** Remove `subtle` from the mod (board, detail view, geometry `Tone`). Every fact draws in the default color. `inactive` is the only grey, used only for labels (`workers`, `as of`), separators and the header counts. No `dimColor`. In the detail view (`unit.tsx`) every `subtle` becomes `inactive` for labels and secondary history, or the default color for facts (plan path, summary, ETA trail, step times, record cells). Ruler: future track `inactive`; now mark `│` in the default color (red is only for late); ETA dot `success` before it is reached, `error` after; ETA label colored like its dot.
11. **Nothing is cut off.** No line that carries text uses `truncate-end` or `fitText` on the board. Move `wrappedLines` from `unit.tsx` to `geometry.ts` and wrap: a title wraps to more lines indented 2 cells under line 1; when the right part of line 2 or 3 does not fit beside the left part with 2 spaces between, it goes on its own next line, indented 2. Every drawn line is at most `width` cells (`cellWidth`).
12. **Height budget** (when the pane is shorter than the board): first drop line 3 of units from the bottom up; then the gaps between units; then whole units from the bottom, named in one `+N more: a, b` line (`doneText`). Wrapped lines count at their real height.

**Files:**
- `mods/unit-board/hooks/board.tsx` — list, block lines, header, height budget.
- `mods/unit-board/hooks/geometry.ts` — `Tone`, `stripCells`, ruler colors, short times, `wrappedLines`.
- `mods/unit-board/hooks/unit.tsx` — colors only, and the shared `wrappedLines`.
- `mods/unit-board/tests/board.test.tsx`, `mods/unit-board/tests/geometry.test.ts` — tests.

**Workers:** 1 writer + 1 tester.
- `impl` — `mods/unit-board/hooks/board.tsx`, `mods/unit-board/hooks/geometry.ts`, `mods/unit-board/hooks/unit.tsx`.
- `test` — `mods/unit-board/tests/board.test.tsx`, `mods/unit-board/tests/geometry.test.ts`, from this Spec alone: units in name order whatever their state; no `open` label and no `Done:`/`Standing by:` line; a running unit with workers and an ETA takes 3 lines; heading has no `/<of>`; a 132-phase plan draws a 20-cell bar and `74/132`; late ETA text and its tone are `error`, `no ETA` is `warning`; worker counts by state with tones; `run ends`/`run ended`/`run ends after G1`; short time forms on the same day, within six days and later; the dot tone for each health case; the header late count; no element anywhere in board or detail view has color `subtle`; at widths 40, 60 and 85 with a long title, a long gate reason and many workers, no drawn line has `…` and none is wider than the width; height budget order. Existing tests that pin the old layout change to the new one.

**Constraints from prior phases:** a closed pane does no work; the mod reads the board JSON and never computes ETAs (relative times like `in 2h 16m` are display arithmetic on recorded times, not predictions); the detail view keeps its fold order from phase 10.

**Acceptance gate:** mod tsc, mod tests, strict validate, the full merge gate.

### Phase 18 — The accounts board fits the pane  · status: done

#### Work Order

**Goal:** `/accounts-board` is readable at every pane width: each account is one short line, colors show how much room is left, and nothing is cut off or broken across columns.

**Spec:**

Source: the user via the showrunner, 2026-10-20: "the /accounts-board can't be read". The cause, found in the code: `AccountsView` draws a fixed table (about 88 columns wide with real roles and credits) and ignores the pane width. In a narrower docked pane, each row wraps through its columns and the table breaks apart. The unit director made the design choices below, to match the phase 17 board.

1. **Use the pane width.** `AccountsView(elements, columns, status, zone, now)` takes `e.props.bodyColumns` from `register.tsx`. Every drawn line is at most `columns` cells (`cellWidth`). No `…`, no `truncate-end`, no `fitText`.
2. **No table.** Remove the heading row and the padded columns (`accountsTable`, `AccountsTable`, `widths`, `width`). Each account is one paired line (move `span`, `wrapLine`, `pairedLines` and their types from `board.tsx` to a shared place both views import, or export them from `board.tsx`; do not copy them):
   - Left: `● <label>`, then `  <roles joined with ', '>` when it has roles. The label is bold.
   - Right: `<n>% left` (rounded), then ` · resets <time>`, then for an account with credits ` · credits <amount|unlimited>` with ` in use` when `use_credits`, then ` · read <time>`. A missing weekly reading is `no reading`; a missing reset, credit or read part is left out (never `—`, never 0).
   - When the right part does not fit beside the left part with 2 spaces between, it goes on its own next line, indented 2, and wraps there (the `pairedLines` rule).
3. **Order:** by label, `localeCompare` with `sensitivity: 'base'`.
4. **Colors** (no `subtle`, no `dimColor`; `inactive` only for the words `resets`, `credits`, `read` and the ` · ` separators):
   - Dot and `<n>% left`: `error` under 10 %, `warning` under 25 %; at 25 % or more the dot is `success` and the text the default color. `no reading`: dot `inactive`, text default.
   - ` in use`: `warning`.
   - Label, roles, numbers and times: the default color.
5. **Short times** in the resolved zone, from `now`: the same local day as now is `14:00`; within six days before or after is `Sat 14:00`; else `24 Oct`. `AccountsPaneState` `accounts` gains `now` (seconds, from `$.clock.now() / 1000`), set at each load; each 10 s tick sets a fresh `now` with no process run (as the board does).
6. **Problems:** after one blank line, `Problems` in bold, then each problem as `• <text>` in `warning`, wrapped at `columns` with continuation lines indented 2.
7. **Nothing else changes:** the argv, the refresh rules, the fixed failure reason and the loading text stay as they are.

**Files:**
- `mods/unit-board/hooks/accounts.tsx` — the view.
- `mods/unit-board/hooks/board.tsx` — export or move the line helpers only; no board change.
- `mods/unit-board/hooks/register.tsx` — pass `bodyColumns` and `now`; set `now` on load and tick.
- `mods/unit-board/types/index.d.ts` — `now` in `AccountsPaneState`.
- `mods/unit-board/tests/accounts.test.ts`, `mods/unit-board/tests/register.test.ts` — tests.

**Workers:** 1 writer + 1 tester.
- `impl` — `accounts.tsx`, `board.tsx`, `register.tsx`, `types/index.d.ts`.
- `test` — `accounts.test.ts`, `register.test.ts`, from this Spec alone: with seven realistic fixture accounts (four Claude, three Codex; roles `showrunners, unit directors`, `workers`, `spare`; credits `unlimited` in use, `42`; one account at 8 %, one at 20 %, one with no reading; long problems) at widths 40, 60, 85 and 120: no line wider than the width, no `…`, every label and every fact present; at 120 each account is one line; at 40 the right part is on its own indented line; accounts in label order; dot and percent tones at 8 %, 20 %, 60 % and no reading; ` in use` is `warning`; no element has color `subtle`; short time forms on the same day, within six days and later; the right part leaves out a missing reset, credit or read; a tick moves `now` with no process run; problems wrap with `• ` and indent 2. Tests that pin the old table change to the new layout.

**Constraints from prior phases:** phases 6, 15 and 16 hold: a closed pane does no work; the mod never opens a credential file and never asks for a login; a failed run shows a fixed reason, never process output. Never capture or read the live accounts pane; check the layout with fixture accounts only. Measure with `cellWidth`, never `.length`.

**Acceptance gate:** mod tsc, mod tests, strict validate, the full merge gate.

### Phase 19 — The accounts board has column headers  · status: done

#### Work Order

**Goal:** `/accounts-board` is a real table again, with a header row and every column aligned, and it still fits the pane and cuts nothing off.

**Spec:**

Source: the user via the showrunner, 2026-10-20, after phase 18 reached main: "it should have column headers and alignment in columns". The showrunner's description of the screenshot: one line per account, no header row; the `resets` values have different widths and Codex rows add a credits part, so the ` · ` separators and `read` sit in different columns; the codex2 line runs to the right edge; under Problems, each line reads `• * claude2: login token expired; …`, with a stray `* `.

1. **Columns.** The columns are a dot column (no header), `Label`, `Roles`, `Left`, `Resets`, `Credits`, `Read`. Each column is as wide as its widest cell or header (`cellWidth`), with 2 spaces between columns and no padding after the last column. `Credits` is shown only when at least one account has a credit balance. The header row is bold, in the default color.
2. **Cells.** No words in the cells: the header names the column. `Label` is bold. `Roles` is the roles joined with `, `. `Left` is `60%` (rounded), or `—` when there is no reading. `Resets` and `Read` are the phase 18 short times (`14:00`, `Sat 14:00`, `24 Oct`). `Credits` is the balance rounded with thousands separators (`13,446`, `42`, `unlimited`), then ` in use` (in `warning`) when the account uses credits. A missing reset, credit or read cell is blank.
3. **Alignment.** `Label`, `Roles` and `Credits` are left-aligned. `Left`, `Resets` and `Read` are right-aligned, so the clock times line up (`Mon 02:00` over `    13:00`). Headers align like their cells.
4. **Colors** do not change from phase 18: the dot and `Left` are `error` under 10 % and `warning` under 25 %; at 25 % or more the dot is `success` and `Left` the default color; no reading gives an `inactive` dot. ` in use` is `warning`. No `subtle`, no `dimColor`.
5. **Forms**, picked once for the whole list:
   - **Table**, when the full table width fits the pane: the header row, then one row per account.
   - **Two-row table**, when it does not: each account takes two rows. Row 1 holds the dot, `Label`, `Roles` and `Left`; row 2, indented 2, holds `Resets`, `Credits` and `Read`. The header is two rows in the same shape. Columns align across accounts in each row kind.
   - **Narrow fallback**, when a row of the two-row table is wider than the pane: the phase 18 narrow form (no header, facts with their words, a break only between facts).
6. **No stray star.** `accounts.py status --json` sends each problem as a plain sentence, without the `* ` list mark that the text table uses (`render_status_json` in `scripts/accounts/accounts.py`). The text table output does not change. The mod draws each problem as `• <text>` as before.
7. **Nothing else changes:** order, short times, problems block, argv, refresh rules, failure text.

**Files:**
- `mods/unit-board/hooks/accounts.tsx` — the table and its forms.
- `scripts/accounts/accounts.py` — `render_status_json` problems only.
- `mods/unit-board/tests/accounts.test.ts`, `scripts/accounts/test_accounts.py` — tests.

**Workers:** 1 writer + 1 tester.
- `impl` — `mods/unit-board/hooks/accounts.tsx`, `scripts/accounts/accounts.py`.
- `test` — `mods/unit-board/tests/accounts.test.ts`, `scripts/accounts/test_accounts.py`, from this Spec alone: pin the exact drawn lines of `readableFixture()` (America/New_York) in the table form at 120 columns, the two-row form at 60 and the narrow fallback at 30; a header row with the six headings (five when no account has credits); every column starts at the same cell in every row; right-aligned `Left`, `Resets` and `Read`; `13,446`; `—` for no reading; blank missing cells; no line wider than the pane and no `…`; the tones from item 4. In `test_accounts.py`, the JSON problems carry no `* ` mark, and the text table still does.

**Constraints from prior phases:** phases 6, 15, 16 and 18 hold. Never capture or read the live accounts pane; check the layout with fixture accounts only. Never run `accounts.py` on real accounts; its tests use their own fixtures.

**Acceptance gate:** mod tsc, mod tests, strict validate, basedpyright on `scripts/accounts`, the full merge gate.

### Phase 20 — The accounts board and cargo-handler show the same table  · status: done

#### Work Order

**Goal:** `/accounts-board` and the cargo-handler accounts table show the same columns, in the same order and format, from the same `accounts.py` JSON.

**Spec:**

Source: the user via the showrunner, 2026-10-20: the two views should "show basically the same thing"; the user likes the green/red mark for "whether or not we are able to access the account"; "in use vs enabled should be columns". The shared spec was settled with cargo-liner's showrunner. handler-enh builds the cargo-handler side (its phase 19) to the same spec. Any later change goes to handler-enh too, so both views change together.

1. **JSON.** `accounts.py status --json` adds two fields to each account (`StatusAccountDocument`, `render_status_json`): `read_ok: bool`, true when the account has a stored reading (`read_at` set) and its latest read outcome is `OK` or `HELD`; `credits_in_use: bool`, true when the account spends credits now (`on_credits == "yes"`). Nothing else in the JSON or the text table changes. Tests in `test_accounts.py` pin both fields in every case (a good read, a failed read, no reading, credits in use, credits enabled and not in use).
2. **Columns, in this order:** the mark (no header), `account`, `role`, `left`, `resets`, `credits`, `enabled`, `in use`, `read`. Headers are all lower case, bold. Every column is always shown.
3. **Cells.**
   - mark: `●`, `success` when `read_ok`, `error` when not. It is the only access signal.
   - account: the label, bold. role: the roles joined with `, `.
   - left: `60%` or `—` with no reading. `error` under 10 %, `warning` under 25 %, else the default color. Low quota colors only this cell.
   - resets: the phase 18 short time. credits: `13,446` or `unlimited`; no ` in use` text in this cell.
   - enabled: `yes` when `use_credits`, else blank. in use: `yes` (in `warning`) when `credits_in_use`, else blank.
   - read: the age of the reading at `now`, floored: `45s` under a minute, `12m` under an hour, `2h` under a day, else `3d`. Blank with no reading.
   - A missing value is blank, except `left`.
4. **Alignment.** `account`, `role`, `enabled` and `in use` are left-aligned. `left`, `resets`, `credits` and `read` are right-aligned, so the `%` signs line up. Headers align like their cells.
5. **Forms** as in phase 19: the table when it fits; else the two-row table (row 1 the mark, `account`, `role`, `left`; row 2, indented 2, `resets`, `credits`, `enabled`, `in use`, `read`, with a two-row header in the same shape); else the narrow form with no header (`● <label>  <roles>`, then the facts with their words: `60% left · resets Sat 14:00 · credits 42 · enabled · in use · read 12m ago`).
6. **Mod types.** `StoredAccount` gains `read_ok` and `credits_in_use`; `AccountDisplay` carries them. A status from an older `accounts.py` with no `read_ok` draws a red mark; with no `credits_in_use`, a blank in use cell.
7. Nothing else changes: order by label, problems block, argv, refresh rules, failure text.

**Files:**
- `scripts/accounts/accounts.py` — `StatusAccountDocument`, `render_status_json`.
- `mods/unit-board/hooks/accounts.tsx`, `mods/unit-board/types/accounts.d.ts` — the table.
- `scripts/accounts/test_accounts.py`, `mods/unit-board/tests/accounts.test.ts`, `mods/unit-board/tests/register.test.ts` — tests.

**Workers:** 1 writer + 1 tester.
- `impl` — `accounts.py`, `accounts.tsx`, `types/accounts.d.ts`.
- `test` — `test_accounts.py`, `accounts.test.ts`, `register.test.ts`, from this Spec alone: the JSON fields in every case above, and that the text table is unchanged; the exact drawn lines of `readableFixture()` (extended with `read_ok` and `credits_in_use`, and one failed read) at 140, 60 and 30 columns; lower case headers in order; mark tones; `left` tones; `yes`/blank; read ages at 45 s, 12 min, 2 h and 3 d; `%` signs in one column; no line wider than the pane and no `…`; an older status without the new fields.

**Constraints from prior phases:** phases 6, 15, 16, 18 and 19 hold. Never capture or read the live accounts pane; never run `accounts.py` on real accounts. accounts-unit owns `accounts.py`; the showrunner gave this edit to unit-board-unit, kept to the JSON document.

**Acceptance gate:** mod tsc, mod tests, strict validate, basedpyright on `scripts/accounts`, the full merge gate.

### Phase 21 — /showrunner:units shows a ticket table for style work  · status: done

#### As-built

- A phase-based production gets `| Unit | Phase | Task |`. Unit holds the session name, `gone`, `not running` or `unknown`. The full-id column is gone.
- A production whose doc has `production_field(lines, "Kind") == "style"` gets `| Unit | Ticket | Step | Built | Left |`, one row per live unit, then one total line. All times are in the production zone.
  - Ticket: the oldest `assigned`/`checkpointed` ticket of the unit, as `#<n> · <rule stem> · <crate> · <count>`. An empty `rule` uses the first `rules` stem plus `+<k>`. Count is findings (proposal), sites (listed) or files (read, structure), singular for 1. With no ticket: `idle — <outcome> (<HH:MM>)` from `refill_results`, or `idle`.
  - Step: the label from `open_step` of the newest run, else `checkpoint sent` for a `checkpointed` ticket, else `—`; ` · ETA <HH:MM>` when `current.eta.time` is set.
  - Built: family findings with `state='built'`, as `<all time> (<n> in 24 h)`, counted by `built_at`. Left: family findings with `state='filed'` and an approved decision (`decision_answer` returns `ApprovedDecision`), as `<total>` only. Each decision file is read once; a built finding with no decision takes the family of its `build_ticket`.
  - Total line: `All families: <built> built · <left> left · <r> built in the last 24 h · ETA at that rate <YYYY-MM-DD>`, or `no ETA: nothing built in 24 h` when r is 0. Numbers use thousands commas.
- Ledger migration 25 adds `refill_results(unit PK, at, outcome, split_at '', ready, changed, structure, other)` and `held_findings.built_at TEXT NOT NULL DEFAULT ''`, backfilled from the merged build ticket's `updated_at`, else the finding's own.
- `style_plan.py main` calls `style_ledger.record_refill(repo, unit, outcome)` after each `next_phase` return, `RepairFirst` and board unavailable included. An exception records nothing. `refill_outcome` gives nine words: `phase written`, `resumed`, `repair first`, `waiting for merge`, `waiting for sync`, `waiting for structure: <crates>`, `waiting for branch-point sync`, `merge the style branch first`, `board unavailable`.
- `built_at` is set to now on each transition to built only when it is `''` (proposal ticket merge, declaration follow, `held built`, `mark_built_at_merge`), and cleared when a finding leaves built.

**Files:**
- `scripts/production/style_units.py` — ticket table, read-only ledger reads, shared step/cell helpers (`OpenStep`, `NoOpenStep`, `open_step`, `run_events`, `session_cell`, `escaped`, `NONE`).
- `scripts/production/units_table.py` — phase table, Kind switch, re-exports of the shared helpers.
- `scripts/style/style_ledger.py` — migration 25, `record_refill`, `built_at` transitions.
- `scripts/style/style_plan.py` — `refill_outcome`, the record call in `main`.
- `pyrightconfig.json` — `scripts/style` on the `scripts/production` paths.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/style-showrunner.md` — both tables, cell forms, total line, `refill_results`, `record_refill`, `built_at`.

**Binds later work:**
- `record_refill(repo, unit, outcome)` writes only `unit`, `at`, `outcome`. It opens its own short connection with the busy timeout, loads no rule catalog, runs no migration, and does nothing when the table is missing.
- The `refill_results` split columns and `split_at` exist with their defaults and have no producer yet; the nightly split command fills them, and `split_at = ''` means no split.
- `style_units.py` opens each ledger read-only (`sqlite3.connect(<uri>?mode=ro, uri=True)`), one connection per distinct database, never through `Ledger`/`open_ledger` (they lock and migrate). A missing database or table reads as no data.
- The shared step/cell helpers live in `style_units.py`; `units_table.py` re-exports them to break a static import cycle. A test that patches `units_table.open_step` does not reach `style_units`.
- The units command never wraps or replaces another module's functions at run time; any cache is an explicit object passed in.
- The live table runs under 3 s (1.5–2.1 s measured).

**Gotchas:**
- A style unit's plan folder comes from its phase record; an unreadable phase record shows `idle` with no ledger read.
- `test_bulk_rename.py` compares the schema version with `max(style_ledger.MIGRATIONS)`, not a fixed number.

**Ruled out:** a classify-only pass inside refill (11–40 min per refill on the live ledger, blob reads in `judged_at_current_file` dominate); a `no work` outcome (`next_phase` never returns `NoAvailableWork`); split text in the Left cell.

### Phase 22 — the ticket table shows each unit's split, read by a nightly command  · status: done

#### As-built

- `style_plan.py split --unit <u> --repo <worktree> [--timeout-minutes <n>]` (default 90) partitions the unit's Left total (filed, approved findings of its family) into `ready`, `changed`, `structure` and `other`. The four counts always add up to the total. Each finding takes the first category that holds it, in the order of the decision loop (`_select_phase`): changed since judged or unsynced file change; rank wait not exempt; available or held by an open build ticket; every other case.
- The scan is read-only. It reads through `style_ledger.preview_ledger`, holds no refill lock, and batches blob reads through a `GitReadCache` (one `git ls-tree -r` per commit) passed to `style_proposals.ProposalGitReader`. Refill and split share one set of rules. The only write is `record_split` at the end, inside the time limit.
- At the time limit, on any error, or when the ledger schema is below 25 (`require_split_table` checks it read-only first), `split` writes nothing, keeps the last split, prints `split <unit>: <cause>` to stderr and exits 1.
- `style_plan.py split-all --production <doc>` runs `split` for each live unit from `production_units.live_unit_table`, one at a time, at nice 19 in `builds.slice`. It prints `<unit>: <ready>/<changed>/<structure>/<other> in <m> min` or `<unit>: failed: <cause>` per unit, prints `no style units` and exits 0 when none exist, and exits 1 when any unit failed.
- `style_ledger.record_split(repo, unit, split)` sets the four counts and `split_at` (scan start, UTC ISO with offset) in `refill_results`. It inserts a row with `at=''` and `outcome=''` when none exists; such a row reads as no refill record. A split reads as `RecordedSplit(ready, changed, structure, other, measured_at)` or `NoSplitRecorded()`. `record_refill` keeps the split columns.
- The Left cell shows `<total>: ready <a> · changed <b> · structure <c> · other <d> (<age>)` with age as `<n> min`, `<n> h` or `<n> d`, else `<total>`. A unit with no refill record and no open ticket shows `idle`, also with a split. `units_table.main` resolves each style unit's ledger folder from the Units row worktree and `unit_plans.unit_plan_path`, not from the phase record.
- The nightly timer (02:00 EDT, `Nice=19`, `builds.slice`, skipped during a build hold) is not in this repository; it belongs in `/etc/nixos`.

**Files:**
- `scripts/style/style_plan.py` — `split`, `split-all`, `classify_split`, `split_time_limit`
- `scripts/style/style_ledger.py` — `record_split`, `RecordedSplit`, `NoSplitRecorded`, `GitReadCache`, `require_split_table`
- `scripts/style/style_proposals.py` — `ProposalGitReader`, `GitProposalReader`
- `scripts/production/style_units.py` — `left_cell` and the split read
- `scripts/production/units_table.py` — `style_ledger_paths` from Units rows
- `scripts/production/unit_table_cells.py` — `OpenStep`, `NoOpenStep`, `session_cell`, `open_step`, `run_events`, `escaped`, `NONE`; `units_table.py`, `style_units.py` and `scripts/direct/board_json.py` import them here, with no re-exports
- `docs/as-built/style-showrunner.md`, `docs/as-built/showrunner-automation.md`, `commands/showrunner/units.md` — split commands, categories and the Left cell

**Gotchas:** The SQLite busy wait delays SIGALRM, so a timed-out write can run up to 30 s past the limit before it rolls back; nothing is written. One split of the types unit took 8.98 min on a copy of the live data. Tests patch the shared cells in `unit_table_cells`, not in `style_units`.

**Ruled out:** Classifying inside refill (11–40 min per unit); wrapping or replacing another module's functions at run time to cache git reads.

### Phase 23 — a timed-out split writes nothing past its limit  · status: todo

**Depends on:** Phase 22

#### Work Order

**Goal:** `style_plan.py split` never writes after its time limit. Today the limit's alarm cannot fire while SQLite waits on a busy ledger, so the final write can run up to 30 s past the limit.

**Source:** phase 22 review finding F005 (minor, open at the phase 22 checkpoint), moved here under the landing rule.

**Spec:**

1. **Deadline.** `split_time_limit` gives the code inside it the deadline (a `time.monotonic()` value) as a domain type, for example `SplitDeadline(at: float)` with `remaining_seconds()`. Do not use a bare float or `None`.
2. **Bounded write.** `style_ledger.record_split(repo, unit, split, *, busy_timeout_seconds: float)` uses that value as the connection's busy timeout in place of `BUSY_TIMEOUT_SECONDS`. `split` passes `min(BUSY_TIMEOUT_SECONDS, deadline.remaining_seconds())`. When no time remains, `split` raises the same `TimeoutError` as the alarm and writes nothing.
3. **Behavior kept.** A busy ledger that stays locked past the deadline makes `split` exit 1 with `split <unit>: <cause>` on stderr and keep the last split. `record_refill` and every other caller keep `BUSY_TIMEOUT_SECONDS`.

**Files:**
- `scripts/style/style_plan.py`
- `scripts/style/style_ledger.py`
- `scripts/style/test_style_plan.py`
- `scripts/style/test_style_ledger.py`

**Workers:** 1 writer.
- `impl` writes items 1–3 and the regression tests: a ledger held by a second connection's open write transaction past a short limit makes `split` fail within about one second of the limit and leaves the last split unchanged; `record_split` with a small busy timeout fails fast on a locked ledger.

**Constraints from prior phases:**
- Phase 22 shipped `split`, `split-all`, `split_time_limit` (SIGALRM) and `record_split`; `record_split` already runs inside the time limit.
- The style ledger is shared with four live style units. Never write to `~/.local/state/style/` or to any plan or decision under `~/rust/`. Never run refill, `next_phase`, `split` or `split-all` against live data; tests use temporary ledgers only.
- The worktree has no `.venv`; basedpyright needs a temporary venv under `/tmp/claude` (`--venvpath`).

**Acceptance gate:**
- basedpyright on the four files, footer `0 errors, 0 warnings, 0 notes`
- `scripts/style`: `test_style_plan`, `test_style_ledger`
- tests show: a split blocked on a locked ledger ends within about one second of its limit and writes nothing; the last split is kept
- the full merge gate
