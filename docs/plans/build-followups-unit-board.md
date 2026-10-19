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
