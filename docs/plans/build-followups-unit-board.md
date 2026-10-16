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
**Depends on:** 2

#### Work Order

**Goal:** `board_json.py` writes one JSON file per production holding everything the board draws, and lists the files whose change makes it stale.

**Spec:**
- CLI: `~/.claude/scripts/lib/py ~/.claude/scripts/delegate/board_json.py (--cwd <path> | --production <slug>)`. It writes each resolved production's file atomically (temp file in the same dir, then `os.replace`) to `${UNIT_BOARD_DIR:-${XDG_STATE_HOME:-~/.local/state}/unit-board}/<slug>.json`, and prints one JSON line `{"files": [<paths>], "focus": <unit key or null>}`. Exit 0 with `"files": []` when nothing resolves.
- **Resolve.** `--cwd`: a cwd inside a unit's worktree (a `Worktree` cell of any registered showrunner's production, `showrunners.registered_showrunners()` + `production_units.live_unit_table`) → that production, `focus` = the unit; inside a showrunner checkout → its production; anything else → every production whose showrunner is running, `focus` null.
- **Per unit** (rows of `live_unit_table`), with the run from `unit_run.local_run(history_root, worktree)` and the session dir `<run_root>/<run_id>` (`unit_runs.run_roots()`, first that exists):
  - `key` (Unit cell less `-unit`), `plan` (Plan cell), `state` (`unit_lookup.run_state`).
  - `phase` `{id, of, title}` of the open phase, or null; `phases`: every plan phase with `status` (`done` | `open` | `todo` | `gated`), `title`, and the phase record from "Predictions chain from the stated ETA" (`took` seconds, `fix_done`, `fix_running`, `findings {blocker, minor, nit}`, `eta {first, trail, finished, delta_minutes}`), the predicted `start`/`finish` for todo phases, and each todo phase's `gate` (below). `status` is `gated` for a todo phase whose gate is not `none`.
  - `finish`: the unit's plan finish, `{"kind": "predicted", "at": <time>}`, `{"kind": "finished", "at": <time>}`, `{"kind": "after_gate", "gate", "waits_on"}` (`GateBlockedFinish`) or `{"kind": "unknown"}`.
  - `stage`: open pass `impl` → `Build`, `test` → `Tests`, `review` → `Review`, `fix` → `Repair round <fix_pass>`; else the open activity's label (`units_table.open_step`); else `Run done`, `Standing by`, or `Waiting on G<k>`.
  - `eta`: `{at, first, trail, moved, delta_minutes, schedule_end, fix_done, fix_running}` for the open phase, or null. `at`, `first` and `trail` are stated promises only; `delta_minutes` is `at` − `first` (the slip so far); `schedule_end` is the end the chain uses, which is now when the promise has passed — never a promise, never a trail entry.
  - `summary`: the newest `progress_reported` `activity` text of the run.
  - `steps`: the open phase instance's passes and activities in order, `[state, what, started, took_seconds]`.
  - `workers`: one per `mesh_roster.json` entry: `slot`, `role`, `state` (its `status`), `model` (`impl_worker_<slot>.json` `family/model`), `doing` (the body of that agent's newest `board.log` line, ≤ 80 chars).
- **Gates.** Each todo phase's `gate` is `{"kind": "none"}`, `{"kind": "timed", "gate": "G<k>", "until": <time>, "waits_on"}` or `{"kind": "untimed", "gate", "waits_on"}`, from `build_plan`'s `NoGate` / `TimedGate` / `UntimedGate`. A cleared gate is `none`.
- **Fix rounds.** `fix_done` counts fix passes that finished, `fix_running` is 1 while one is open, else 0. Split the phase record's `fix_rounds` in `phase_table` into these two (and keep every existing reader's meaning: the sum).
- **Times.** Every time is `{"at": <epoch seconds>, "text": "<HH:MM>"}` in the production zone, `text` `ddd DD Mon HH:MM` when not today. Top level: `production`, `as_of`, `zone`, `utc_offset_minutes` (at `as_of`, for the mod's `now` label), `units`, and `sources`.
- **`sources`:** absolute paths whose mtime moving means the file is stale: each unit's run `.jsonl`, `mesh_roster.json`, `board.log`, every `impl_worker_<slot>.json`, plan doc, every run `.jsonl` of the plan the predictions read (`plan_history.plan_runs`), and the production doc; plus, for discovery, each unit's session dir and the history `runs/` dir, whose mtimes move when a run or file is created there, so a pane opened before a run exists sees it start. Only paths that exist.
- **Types.** The JSON shape is also written as TypeScript in `mods/unit-board/types/board.d.ts` (the mod phase reads it), field for field, so the two cannot drift silently; the test below checks the field names against it.
- No recorder change: the recorder does not write the board file (the mod asks for it while open).
- Cost: one run reads only the production's units' files. Log nothing.

**Files:**
- `scripts/delegate/board_json.py` — new.
- `scripts/delegate/test_board_json.py` — new.
- `mods/unit-board/types/board.d.ts` — new, the JSON contract.
- `scripts/delegate/phase_table.py` — the `fix_done`/`fix_running` split, and a public accessor if the record needs one.
- `scripts/delegate/test_phase_table.py` — the split.

**Workers:** 1 writer + 1 tester
- `impl` — `board_json.py`, `types/board.d.ts`, `phase_table.py`; hub: `mods/unit-board/types/board.d.ts` (the contract the test checks).
- `test` — `test_board_json.py` and the split's cases in `test_phase_table.py`: a temp production doc, a showrunner conf under `NOTIFIER_STATE_DIR`, a unit worktree with a plan, a run stream under `PLAN_DELEGATE_HISTORY_DIR` and a session dir with `mesh_roster.json`, `impl_worker_1.json` and `board.log`: `--cwd <worktree>` focuses the unit; `--cwd /` lists every running production; stages for each pass kind; timed, untimed and cleared gates, each with and without an open phase, and every `finish` kind; an overdue unchanged promise adds no `trail` entry and its `schedule_end` is now; a running fix counts in `fix_running` only; `sources` lists only existing files; with no run yet, creating the run and session files moves a listed dir's mtime; the write is atomic (no partial file on a failed write); every key in the output appears in `types/board.d.ts`.

**Constraints from prior phases:** stated ETAs select by `eta_phase_id`; `phase_table` builds the chain with gates and exposes the per-phase record (`first_eta`, `moves`, `eta_trail`, `finished_at`, `delta_minutes`, `fix_rounds`, `findings`), read only from the row's own title family (a retired task's attempts under a reused number are excluded). The plan readers live in `scripts/delegate/plan_history.py` (`plan_runs`, `plan_phases`, `PlanPhase`, `history_root`, `now_epoch`, `resolve_plan_path`, `eta_band_seconds`); `phase_table` imports it, never `progress_history`; tests patch `plan_history.now_epoch` for the clock. Todo rows carry `gate`: `NoGate`, `TimedGate(gate, until, waits_on)` or `UntimedGate(gate, waits_on)`; `plan_finish` may be `GateBlockedFinish(gate, waits_on)`. An overdue open phase's `StatedEta.time` is already advanced to now for scheduling, while `eta_trail` keeps the real promises. `phase_table show --json` omits gates and turns `GateBlockedFinish` into null, so build from `build_plan`'s record, not that JSON.

**Acceptance gate:** the Test and Lint lines of Delegation Context green; `board_json.py --production build-followups` on natedev writes a file whose units match `/showrunner:units`.

### Phase 4 — The board mod  · status: todo
**Depends on:** 3

#### Work Order

**Goal:** `/unit-board` opens a pane showing the production board (or, in a unit worktree, that unit), it stays current while open, and nothing runs or shows while it is closed.

**Spec:**
- **Plugin.** `mods/unit-board/.claude-plugin/plugin.json` (`name: unit-board`, `version: 0.1.0`), `hooks/hooks.json` `{"modules": ["./register.tsx"]}`, `tsconfig.json` as `mods/postmaster/tsconfig.json` (with `.tsx` in `include` and `jsx` settings from the prototype). Local marketplace `mods/.claude-plugin/marketplace.json` listing `unit-board` (and `postmaster`, unchanged in behaviour).
- **Modules.** `hooks/register.tsx` holds only wiring; `hooks/data.ts` (generator call, file read, stat refresh), `hooks/geometry.ts` (strip, ruler, label runs), `hooks/board.tsx` (A), `hooks/unit.tsx` (B). Types from `types/board.d.ts`; plugin state declared in `types/index.d.ts`.
- **Pane state.** Each pane owns its state, keyed by pane id, as a tagged union, never a nullable board plus a string: `{kind: "loading"}`, `{kind: "board", data}`, `{kind: "unit", data, unit}`, `{kind: "failed", reason}`; a closed pane has no entry. Each open pane owns its own timer handle. `hooks/data.ts` exports one stat-and-run helper that takes a pane's paths, generator and state setter, so a second pane (Phase "Accounts view") reuses it without sharing a timer or state with this one.
- **Commands.** `session.start` only registers `/unit-board` and, when `$.ui.list()` shows the pane already open (a module reload), restarts the refresh; nothing else.
  - `/unit-board` — run the generator once (`$.process.run([py, board_json.py, "--cwd", cwd])`, `py` = `$HOME/.claude/scripts/lib/py`), read its files, open pane `unit-board` (title `Units`): B for `focus`, else A.
  - `/unit-board <unit>` — B for that unit. `/unit-board close` — close the pane. The command result text is one short line (`Unit board opened.`).
  - In the pane: each unit's `open` button shows B; B has a `board` button back to A.
- **Refresh while open only.** On open start `$.clock.every(10_000)`: `$.fs.stat` each path in `sources` (files and the discovery dirs) and the board file; if any mtime is newer than the board file, run the generator once, then `$.fs.read` and `$.state` update (a redraw follows the state write). A missing board file runs the generator once; a `sources` path that has disappeared counts as a change. `turn.complete`: when open, the same check; when closed, `next(e)` at once. `ui.close` (any origin, `plugin`, `person`, `unload`): cancel that pane's timer, drop its state and pass to `next`.
- **Clock while open.** Each tick also moves the ruler's `now` mark from the engine clock, with no file read and no generator. Predictions belong to the board file's `as_of` snapshot, which the header shows; the mod never recomputes an ETA, and a promise that has passed stays drawn as recorded until the generator's next run.
- **A (board).** Per production: header (slug, unit count, `as of HH:MM`), then per unit: `● key  Phase n/of  title  open`; the phase strip (█ done, ▓ open, ░ todo, ▒ gated; above 30 phases show counts `44/45 done` instead of shrinking); stage; ETA line from `eta` (`ETA 18:30  first 17:15 · moved 2× · +1:15 late · fix rounds 1 + 1 running` from `fix_done` + `fix_running`, or `on its first ETA`); the next gated phase's line from its `gate` (`timed`: `Phase n gated until Wed 22 Oct 11:47 — G3: <waits_on>`; `untimed`: `Phase n gated by G3: <waits_on>`); the unit's finish from `finish` (`done by 21:40`, `finished 17:02`, `after G3`, or nothing for `unknown`); the ruler; a workers line (`1 fix running · 2 review idle`). Layout as `mockup-A-board.txt`.
- **B (unit).** Summary; ETA and its trail (`17:15 said 14:42 → 17:45 said 14:48 → 18:30 said 15:03`); ruler; the current phase's steps with times; workers table; phase record rows (`took`, fix rounds as dots, `-1:13 early, said 14:15`). Layout as `mockup-B-unit.txt`.
- **Ruler** (geometry.ts, pure): window from phase start to max(ETA, now) + 10 %; elapsed `━`, earlier ETAs `○` (`eta.trail`), current ETA `●` (`eta.at`, the promise, never `schedule_end`), now a red `│`; labels that would overlap are dropped, later mark wins; `now` is the engine clock, labelled with `utc_offset_minutes`.
- **Colours** are theme keys only (`success`, `error`, `warning`, `suggestion`, `inactive`, `subtle`).
- **No band, no status line, no toast** (Invariants).
- **Tests** (`tests/*.test.ts`, the postmaster kit style): mount on `terminal` and `desktop`; `/unit-board` opens A and, with a `focus`, B; `open`/`board` presses switch views; the generator is called with exactly `[py, board_json.py, "--cwd", cwd]`; a changed source mtime runs the generator once and redraws; a new mtime on a discovery dir (a run started after open) does the same; an unchanged one reads nothing; advancing the fake clock past a promised ETA with unchanged files moves the `now` mark, keeps the promise drawn and runs no generator; timed, untimed and cleared gates and each `finish` kind draw their lines; closed → no `fs`, `process` or timer call on `turn.complete` or over a fake-clock minute; `ui.close` cancels the timer and drops the pane's state; geometry cases for the ruler window, label dropping and the strip's count form.
- **Install.** The user runs once per machine: `/plugin marketplace add ~/.claude/mods` then `/plugin install unit-board` at user scope. The unit director adds that to the change review (`/change_review`) with the live check below; it never edits `settings.json`.
- **Live check** (unit director): load with `command claude --plugin-dir <worktree>/mods/unit-board` in a throwaway tmux session on a private socket (`tmux -L unitboard`), at 170 columns: `/unit-board` from the trunk checkout draws A with real data, from this worktree draws B; close it and confirm with a fake source touch that no generator runs while closed. Capture both panes as text. The phone check (Remote Control, `/unit-board` from the phone) goes to the change review for the user.

**Files:**
- `mods/unit-board/.claude-plugin/plugin.json`, `mods/unit-board/hooks/hooks.json`, `mods/unit-board/tsconfig.json` — the plugin's manifest, module list and type-check config.
- `mods/unit-board/hooks/register.tsx` — wiring only: commands, open/close, refresh.
- `mods/unit-board/hooks/data.ts`, `mods/unit-board/hooks/geometry.ts` — load and refresh; strip, ruler and runs.
- `mods/unit-board/hooks/board.tsx`, `mods/unit-board/hooks/unit.tsx` — views A and B.
- `mods/unit-board/types/index.d.ts` — plugin state.
- `mods/unit-board/tests/` — geometry and register tests.
- `mods/.claude-plugin/marketplace.json` — the local folder marketplace.

**Workers:** 1 writer + 1 tester
- `impl` — every file except `tests/`; hub: `mods/unit-board/types/index.d.ts` (plugin state both sides use).
- `test` — `tests/geometry.test.ts` from the ruler and strip rules above, and `tests/register.test.ts` from the command, refresh and closed-quiet rules, against `types/board.d.ts` fixtures.

**Constraints from prior phases:** the generator CLI, output line `{"files", "focus"}`, file location and JSON contract `mods/unit-board/types/board.d.ts` (Phase "Board data"); times are `{at, text}`; `sources` lists the files and discovery dirs to stat. Todo phases carry a tagged `gate` (`none`, `timed`, `untimed`) and each unit a tagged `finish` (`predicted`, `finished`, `after_gate`, `unknown`); `eta.at`, `eta.first` and `eta.trail` are promises, `eta.schedule_end` is not; fix rounds come as `fix_done` and `fix_running`.

**Acceptance gate:** `tsc`, `command claude plugin validate --strict mods/unit-board` and `command claude plugin test mods/unit-board` exit 0; the live check's captures match the mockups' layout.

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

**Constraints from prior phases:** the phase record (`first_eta`, `moves`, `eta_trail`, `delta_minutes`, `fix_done`, `fix_running`) and the gate text come from `phase_table` (`build_plan`, gates `NoGate`/`TimedGate`/`UntimedGate`); plan readers from `plan_history`, and tests patch `plan_history.now_epoch`. An overdue open phase's `StatedEta.time` is the schedule end, not a promise. The board command is `/unit-board`.

**Acceptance gate:** the Test and Lint lines of Delegation Context green.

### Phase 6 — Accounts view  · status: todo
**Depends on:** 4

#### Work Order

**Goal:** `/accounts-board` opens a pane showing the units/showrunners line, the accounts table with each login's address, and the problems under it, drawn from disk straight to the screen, current while open, with nothing running while closed.

**Spec:**
- **Data source.** two-accounts-unit's stored status with logins is promoted (49fde9b3; the showrunner, 2026-10-16): `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py status --json --stored --with-login`. It prints `{"accounts": [{label, tool, roles[], weekly_left_percent, resets_at, credits, use_credits, read_at, login}], "problems": [...]}`. `--stored` reads stored readings only — no live read, no subprocess, no `ssh`; `--with-login` needs `--json`. Never parse the text table. Copy the exact field types into `mods/unit-board/types/accounts.d.ts` from the code on the merge branch. Every run this phase makes, the open run included, is exactly that command. This phase does not edit `accounts.py`.
- **The mod never opens a credential file** (the showrunner, 2026-10-16: those files are read for nothing beyond what they must be): no read of `~/.claude<N>/.claude.json`, any `auth.json` or any `id_token`, not even to decode the email. Login addresses come only from the `login` field of `--with-login`, and are only drawn: never in a command result, `$.ui.log`, a toast or a message.
- **Mod** (`hooks/accounts.tsx`, wiring in `register.tsx`): `session.start` also registers `/accounts-board`, and restarts its refresh when `$.ui.list()` shows the pane open. `/accounts-board` runs the command once, opens pane `accounts-board` (title `Accounts`), and draws: the units/showrunners line built from the `roles` fields; the table (Label, Login, Roles (comma-joined), Weekly left, Weekly resets, On credits, Credits left, Read; weekly left under 10 % `error`, under 25 % `warning`); the problems under it. `/accounts-board close` closes it. The command result text is `Accounts opened.` only.
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

**Acceptance gate:** `tsc`, `command claude plugin validate --strict mods/unit-board` and `command claude plugin test mods/unit-board` exit 0; the live check above, both panes open and each closed in turn.
