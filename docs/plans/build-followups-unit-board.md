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

The live check on 2026-10-16 found `/unit-board` in the trunk failing with "process.run … still running after 30000ms". `board_json.py` took 35 s for three productions under load. 90% of that was `unit_run.local_run`, which parsed every line of all 184 run files once per unit (1.16M JSON parses for 22 units).

#### Work Order

**Goal:** `board_json.py --cwd ~/.claude` finishes in a few seconds, so the trunk board opens within the mod's 30 s process limit even under load.

**Spec:** `scripts/production/unit_run.py` `local_run` stops reading a run file at its `run_started` event, matching or not; a run file holds exactly one, on line 1 (checked across all 184 files). Lines that do not contain `"run_started"` are skipped before `json.loads`. Results are unchanged.

**Files:**
- `scripts/production/unit_run.py` — `local_run`.

**Workers:** none. The showrunner approved a director-written fix (2026-10-16), already in the tree.

**Constraints from prior phases:** none beyond Delegation Context.

**Acceptance gate:** `scripts.production.test_unit_run` and `scripts.delegate.test_board_json` green; `merge_gate --tests-only` green; basedpyright clean on `unit_run.py`; `board_json.py --cwd ~/.claude` 35 s → under 5 s.

### Phase 8 — Board layout fixes

The design check of the live board (fresh reviewer, 2026-10-16) said "ship after these fixes". The showrunner made them a phase of this run.

#### Work Order

**Goal:** the production board shows every unit at a glance, with one style and no empty or repeated lines. The unit view lines up and shows a finished unit's phases.

**Spec:**

Board (`mods/unit-board/hooks/board.tsx` `BoardView`):

1. Order and size. Units sort as running, then standing by (any other state), then run done, keeping the JSON order within each group. A unit whose `state` is `run-finished` draws one line: dot, key, then `Run done` and its `finishText` when non-empty, right-aligned `open` button. It has no strip, stage, ETA, ruler or workers lines. With 22 units, mostly done, the whole production then fits a 50-row pane.
2. No phase, no repetition. When `unit.phase` is null, the header line reads `● <key>  <stage>` (no `no phase running`), and no separate stage line is drawn. With a phase, the header stays `● <key>  Phase i/n  <title>` and the strip line carries the stage.
3. Strip separator. The two spaces before the stage on the strip line print only when the strip has cells. No line starts with 4 spaces of indent.
4. Empty roles. `workersText` joins `[count, role, state]` with single spaces, skipping empty parts: `2 paused`, never `2  paused`.
5. Predicted finish with no phase running. `done by HH:MM` under a unit with no phase running keeps its text only when a later phase is predicted. Otherwise `finishText` is not drawn for that unit. The JSON already says which: a `todo` or `gated` row in `phases`.

Worker states (`scripts/delegate/board_json.py` `workers`):

6. One word per state. Map the roster `status` to `running`, `paused`, `idle` (roster `done`: the turn finished, the thread is open) and `ended` (the thread is closed). Any other value passes through. Units with all workers `ended` draw no workers line on the board.

Unit view (`mods/unit-board/hooks/unit.tsx` `UnitView`):

7. No blank block. The ETA/gate/finish/ruler/trail box and the strip box render only when at least one child has content, so a finished unit shows no blank lines under `as of`.
8. Aligned workers. Pad `slot` to the widest slot in the list plus one space, the role to the widest role plus one, and the state to the widest state plus one.
9. Worker activity text. `doing` (`board_json.py` `workers`, from the newest `board.log` line) drops a leading `launcher: ` and any trailing `; summary at …` / `; findings in …` clause, so the row reads `adversary review finished`.
10. Phase record of a finished unit. When the plan doc resolves to no phases, `board_json.py` `unit` builds `phases` from the found run's own events (`units_table.run_events`). There is one `done` row per `phase_finished` (id `phase_id`, title `phase_title`, took `phase_elapsed_seconds`). `fix_done` counts that instance's repair passes, `findings` counts its `finding_opened` by severity, and `eta` comes from its `eta_stated` events by `eta_phase_id` (first, trail, finished = `phase_finished` time, delta). An instance with `phase_started` and no `phase_finished` is `open`. Rows keep the `BoardPhase` contract in `mods/unit-board/types/board.d.ts`, so the mod needs no change for it. `phase.of` and the strip follow from those rows. A unit with no plan phases and no run phases draws `Phase record` with one subtle line, `no phases recorded`.

Keep every Delegation Context invariant: nothing opens by itself, no work while closed, the mod never computes ETAs.

**Files:**
- `mods/unit-board/hooks/board.tsx` — items 1–5.
- `mods/unit-board/hooks/unit.tsx` — items 7, 8, 10 (empty record line).
- `scripts/delegate/board_json.py` — items 6, 9, 10.
- `scripts/delegate/test_board_json.py` — tests for 6, 9, 10.
- `mods/unit-board/tests/board.test.tsx` — new: render tests for 1–5, 7, 8 using the kit in `tests/kit.ts`.

**Workers:** 1 writer + 1 tester.
- `impl` — `board.tsx`, `unit.tsx`, `board_json.py`.
- `test` — `test_board_json.py` and `tests/board.test.tsx`, written from this Spec alone.

**Constraints from prior phases:**
- Phase 7: `unit_run.local_run` reads each run file only to its `run_started` line.
- The JSON contract is `types/board.d.ts` field for field, and `test_board_json.py` checks every output key against it. A changed field changes both.
- Board JSON times are `BoardTime {at, text}` in the production zone; Python writes them with `astimezone(user_zone())`.

**Acceptance gate:**
- `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/unit-board` clean.
- `command claude plugin test mods/unit-board` and `command claude plugin validate --strict mods/unit-board` green.
- `basedpyright scripts/delegate` reports 0 errors, 0 warnings.
- `merge_gate --tests-only` green.
- Live (director): trunk `/unit-board` shows all of build-followups' units in one 50-row pane at 170 columns. `/unit-board` in this worktree shows phases 1–8 under `Phase record`. A fresh design reviewer passes both captures.
