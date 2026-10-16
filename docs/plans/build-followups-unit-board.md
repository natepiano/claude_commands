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
  - `scripts/delegate/` — `progress_history.py` (recorder, 4970 lines), `phase_table.py` (phase table and vault note, 1711 lines), `findings.py`, new `board_json.py`; tests beside them.
  - `scripts/production/` — `production_units.py`, `unit_run.py`, `unit_lookup.py`, `units_table.py`, `showrunners.py` (read only by this plan's code).
  - `scripts/accounts/` — `accounts.py` and its notes readers (the accounts view's data).
  - `mods/unit-board/` — new mod. `mods/postmaster/` is the house example of a mod: copy its `tsconfig.json`, `.claude-plugin/plugin.json` shape and test style (`tests/kit.test.ts`, `tests/register.test.ts`).
  - Reference only, outside the repo: the prototype `~/.local/state/plan-backlog/2026-10-16-unit-report-mod/prototype/unit-board/` (`hooks/register.tsx`, `types/index.d.ts`, `data/board.json`), the mockups `mockup-A-board.txt` and `mockup-B-unit.txt` beside it (open the `.ansi` twins with `less -R`), and the engine API `prototype/source/claude-code.d.ts`, `reference.md`, `SKILL.md`, `examples/pane.tsx`.
- **Key files:**
  - `scripts/delegate/progress_history.py` — subcommands at `_build_parser` 4774–4945; `eta` subcommand 4891, `_eta` 4506–4545 writes `eta_stated` (`eta_at`, `basis`, `eta_earliest_at`, `eta_latest_at`, epochs) and refuses with no active phase; `_local_eta_epoch` 402; `_event` 1183 (common fields); `_append_event` 498; `_write_state` 439 (atomic); `_refresh_phase_table` 455 (subprocess, 10 s timeout, only for plans holding `> **Production:`); `_summary_table` 4025–4068 (Project and Phase rows), `_eta_cell` 3735, `_eta_seconds` 2238 (`elapsed*(100-percent)/percent`), `_eta_band_cells` 3760, `eta_band_seconds` 3802; `_report_progress` 4260 (assembles the clocks table and round table at 4458–4481); `_print_last_recorded` 4207; `_phase_section` 4071, `_round_table` 3469; `BLOCKED_BY_PATTERN` 113, `PlanPhase.blocked_by` 703–711; history root `_history_root()` 235 (`$PLAN_DELEGATE_HISTORY_DIR`, else `~/.local/state/plan-delegate`), events in `runs/<run_id>.jsonl`, `run_id` = session dir name.
  - `scripts/delegate/phase_table.py` — imports `progress_history` at module level (line 26), so `progress_history` may import `phase_table` only inside a function. `_newest_open_instance` 440–497 (latest `eta_stated` when still in the future, else `_reported_eta` 653–679); `_latest_stated_eta` 705; `_done_phase` 745; `_plan_finish` 783; `build_plan(plan_path)` 815–947 (todo phases laid back to back from the open phase's ETA or now, 907–936; `typical` 888–904; gap from `_gap_samples` 588); `build(session_dir)` 950; `_production_unit_plans` 1207; `show_production(doc)` 1221; `PRODUCTION_PATTERN` 1236, `_production_reference` 1243; `_note_text` 1396; `_write_note` 1453 (atomic); `_vault_root` 1508; `refresh(session_dir)` 1595–1641; `_parallel_check` 516–547.
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

#### Work Order

**Goal:** every ETA a unit director states — in a checkpoint notice, an `/unit:eta` answer or any message — is an `eta_stated` event for the phase it names, including a phase that has not started yet.

**Spec:**
- `progress_history.py eta` gains `--phase-id <id>`. Every `eta_stated` event gains `eta_phase_id`: the given id, else the active phase's id. With no active phase and no `--phase-id`, keep today's refusal.
- An ETA for a phase other than the active one (the checkpoint notice's `Phase <next> ETA`, sent after the code commit and before the next `start-phase`) is recorded with that phase id. The event still carries the common `_event` fields; the phase fields describe the active phase if any, so readers use `eta_phase_id`, never `phase_id`, to place an ETA.
- A past `--time` is still refused. `basis` values used by callers: `checkpoint` (the notice), `eta-command` (`/unit:eta`), `stated` (any other message).
- `phase_table._newest_open_instance` and `_latest_stated_eta` select `eta_stated` events by `eta_phase_id` (falling back to `phase_id` for events written before this phase, which lack `eta_phase_id`). An event recorded before its phase started applies to that phase's instance once it starts.
- `docs/production_format.md` item 3 and `docs/delegate/phase_end.md` step 2: before sending the checkpoint notice, run `~/.claude/scripts/lib/py ~/.claude/scripts/delegate/progress_history.py eta --session-dir <SESSION_DIR> --phase-id <next> --time <HH:MM> --basis checkpoint`; the notice's ETA is the recorded one.
- `commands/unit/direct.md` `<ProgressContract>` (586–622): one rule — every ETA the unit director states anywhere (a message to the showrunner, a reply, a notice) is recorded first with `progress_history.py eta` (`--phase-id` when it names a phase other than the active one), and states the time the recorder printed.
- `commands/unit/eta.md` and `commands/unit/eta_breakdown.md`: pass `--basis eta-command`.

**Files:**
- `scripts/delegate/progress_history.py` — `--phase-id` on `eta`; `eta_phase_id` on `eta_stated`.
- `scripts/delegate/phase_table.py` — select stated ETAs by `eta_phase_id`.
- `scripts/delegate/test_progress_history.py`, `scripts/delegate/test_phase_table.py` — cases below.
- `docs/production_format.md`, `docs/delegate/phase_end.md`, `commands/unit/direct.md`, `commands/unit/eta.md`, `commands/unit/eta_breakdown.md` — the recording rule.

**Workers:** 1 writer + 1 tester
- `impl` — every file above except the two tests; hub: `scripts/delegate/progress_history.py` (the event shape both tests read).
- `test` — in `test_progress_history.py`: `eta --phase-id 3` with phase 2 active writes `eta_phase_id: "3"`; no active phase and no `--phase-id` refuses; `eta_phase_id` defaults to the active phase. In `test_phase_table.py`: an ETA recorded for phase 3 before `start-phase 3` is phase 3's ETA once it starts and is not phase 2's; an old event with only `phase_id` still counts. Use `recorder_fixture.invoke_recorder` and the env overrides `PLAN_DELEGATE_HISTORY_DIR`, `PLAN_DELEGATE_NOW_EPOCH`, `PHASE_TABLE_VAULT`, `PLAN_DELEGATE_SYNC_WRITES=0`, `USER_ZONE=UTC` as the existing `setUp`s do.

**Constraints from prior phases:** none.

**Acceptance gate:** the Test and Lint lines of Delegation Context green; the new cases pass.

### Phase 2 — Predictions chain from the stated ETA  · status: todo
**Depends on:** 1

#### Work Order

**Goal:** the vault note's predictions and the recorder's Project row agree with the stated ETAs: todo phases chain from the open phase's stated ETA, a gated phase shows its gate, and the project ETA is the chain's finish instead of wall-clock percent arithmetic.

**Spec:**
- **Open phase end.** The latest `eta_stated` for the open phase when still in the future; when it has passed and the phase is still open, `now` (the phase is late; it never ends in the past); with no stated ETA, today's `_reported_eta` projection.
- **Chain.** `build_plan` lays todo phases back to back from that end as today (907–936), each `typical` long with `gap` between.
- **Gates.** A todo phase with `**Blocked by:** G<k>` (`PlanPhase.blocked_by`) reads row `G<k>` of the production doc's `## Gates` table (columns `Gate | Waiting | Waits on | Clears when`; the production doc comes from `_production_reference`). A row whose `Clears when` starts with `cleared` is open. Otherwise:
  - a `Clears when` cell holding a time `YYYY-MM-DD HH:MM` (production zone; first match) moves that phase's start to no earlier than that time; the row prints `gated until <ddd DD Mon HH:MM>`;
  - with no time, that phase and every later one have no predicted time; each row prints `gated by G<k>: <Waits on cell>`.
  - A plan outside a production has no gates table: `Blocked by` is ignored as today.
- **Project row** (`progress_history._summary_table`): ETA = the chain's finish (`_plan_finish`); ETA low/high = that finish shifted by the open phase's band (finish − (open ETA − open low), finish + (open high − open ETA)). With an untimed gate in the chain the ETA cell reads `after G<k>` and low/high read `—`. Percent and elapsed stay as they are. `progress_history` imports `phase_table` inside the function (phase_table imports progress_history at module level).
- **Phase record** (used by the board in "Board data"): `phase_table` exposes per phase, done or open, `first_eta` (first `eta_stated` for it), `moves` (count of later `eta_stated` whose time differs), `eta_trail` (`[(eta_at, stated_at)]`), `finished_at` (done only), `delta_minutes` (finish − last ETA, done only), `fix_rounds` (count of `pass_started` with `pass_kind == "fix"` in its instances), `findings` (counts of `finding_opened` by `SEVERITIES`). Put it on the existing per-phase record type that `build_plan` returns; the vault note does not print it in this phase.

**Files:**
- `scripts/delegate/phase_table.py` — open-phase end, gates, phase record.
- `scripts/delegate/progress_history.py` — Project row from the chain.
- `scripts/delegate/test_phase_table.py`, `scripts/delegate/test_progress_history.py` — cases below.

**Workers:** 1 writer + 1 tester
- `impl` — `phase_table.py`, `progress_history.py`; hub: `scripts/delegate/phase_table.py` (the record type both tests read).
- `test` — open phase with a stated ETA at 18:30 and two todo phases: predictions start at 18:30 plus gap; a passed stated ETA chains from now; a timed gate row moves the gated phase to its time and prints `gated until`; an untimed gate prints `gated by G<k>` for it and later phases, and the Project row reads `after G<k>`; a cleared gate is ignored; the Project ETA equals the last predicted finish (not percent arithmetic); the phase record's `moves`, `delta_minutes`, `fix_rounds` and `findings` from a scripted event stream.

**Constraints from prior phases:** `eta_stated` carries `eta_phase_id` (Phase "Every stated ETA is recorded"); select by it, falling back to `phase_id`.

**Acceptance gate:** the Test and Lint lines of Delegation Context green; for mul_add's 2026-10-16 shape (Phase 5 stated 17:15 → 17:45 → 18:30) the vault note predicts Phase 6 after 18:30.

### Phase 3 — Board data  · status: todo
**Depends on:** 2

#### Work Order

**Goal:** `board_json.py` writes one JSON file per production holding everything the board draws, and lists the files whose change makes it stale.

**Spec:**
- CLI: `~/.claude/scripts/lib/py ~/.claude/scripts/delegate/board_json.py (--cwd <path> | --production <slug>)`. It writes each resolved production's file atomically (temp file in the same dir, then `os.replace`) to `${UNIT_BOARD_DIR:-${XDG_STATE_HOME:-~/.local/state}/unit-board}/<slug>.json`, and prints one JSON line `{"files": [<paths>], "focus": <unit key or null>}`. Exit 0 with `"files": []` when nothing resolves.
- **Resolve.** `--cwd`: a cwd inside a unit's worktree (a `Worktree` cell of any registered showrunner's production, `showrunners.registered_showrunners()` + `production_units.live_unit_table`) → that production, `focus` = the unit; inside a showrunner checkout → its production; anything else → every production whose showrunner is running, `focus` null.
- **Per unit** (rows of `live_unit_table`), with the run from `unit_run.local_run(history_root, worktree)` and the session dir `<run_root>/<run_id>` (`unit_runs.run_roots()`, first that exists):
  - `key` (Unit cell less `-unit`), `plan` (Plan cell), `state` (`unit_lookup.run_state`).
  - `phase` `{id, of, title}` of the open phase, or null; `phases`: every plan phase with `status` (`done` | `open` | `todo` | `gated`), `title`, and the phase record from "Predictions chain from the stated ETA" (`took` seconds, `fix_rounds`, `findings {blocker, minor, nit}`, `eta {first, trail, finished, delta_minutes}`), and the predicted `start`/`finish` for todo phases.
  - `stage`: open pass `impl` → `Build`, `test` → `Tests`, `review` → `Review`, `fix` → `Repair round <fix_pass>`; else the open activity's label (`units_table.open_step`); else `Run done`, `Standing by`, or `Waiting on G<k>`.
  - `eta`: `{at, first, moved, delta_minutes, fix_done, fix_running, gate}` for the open phase.
  - `summary`: the newest `progress_reported` `activity` text of the run.
  - `steps`: the open phase instance's passes and activities in order, `[state, what, started, took_seconds]`.
  - `workers`: one per `mesh_roster.json` entry: `slot`, `role`, `state` (its `status`), `model` (`impl_worker_<slot>.json` `family/model`), `doing` (the body of that agent's newest `board.log` line, ≤ 80 chars).
- **Times.** Every time is `{"at": <epoch seconds>, "text": "<HH:MM>"}` in the production zone, `text` `ddd DD Mon HH:MM` when not today. Top level: `production`, `as_of`, `zone`, `utc_offset_minutes` (at `as_of`, for the mod's `now` label), `units`, and `sources`.
- **`sources`:** absolute paths whose mtime moving means the file is stale: each unit's run `.jsonl`, `mesh_roster.json`, `board.log`, plan doc, and the production doc. Only paths that exist.
- **Types.** The JSON shape is also written as TypeScript in `mods/unit-board/types/board.d.ts` (the mod phase reads it), field for field, so the two cannot drift silently; the test below checks the field names against it.
- No recorder change: the recorder does not write the board file (the mod asks for it while open).
- Cost: one run reads only the production's units' files. Log nothing.

**Files:**
- `scripts/delegate/board_json.py` — new.
- `scripts/delegate/test_board_json.py` — new.
- `mods/unit-board/types/board.d.ts` — new, the JSON contract.
- `scripts/delegate/phase_table.py` — only if the phase record needs a public accessor.

**Workers:** 1 writer + 1 tester
- `impl` — `board_json.py`, `types/board.d.ts`, `phase_table.py`; hub: `mods/unit-board/types/board.d.ts` (the contract the test checks).
- `test` — `test_board_json.py`: a temp production doc, a showrunner conf under `NOTIFIER_STATE_DIR`, a unit worktree with a plan, a run stream under `PLAN_DELEGATE_HISTORY_DIR` and a session dir with `mesh_roster.json`, `impl_worker_1.json` and `board.log`: `--cwd <worktree>` focuses the unit; `--cwd /` lists every running production; stages for each pass kind; a gated phase; `sources` lists only existing files; the write is atomic (no partial file on a failed write); every key in the output appears in `types/board.d.ts`.

**Constraints from prior phases:** stated ETAs select by `eta_phase_id`; `phase_table` builds the chain with gates and exposes the per-phase record (`first_eta`, `moves`, `eta_trail`, `finished_at`, `delta_minutes`, `fix_rounds`, `findings`).

**Acceptance gate:** the Test and Lint lines of Delegation Context green; `board_json.py --production build-followups` on natedev writes a file whose units match `/showrunner:units`.

### Phase 4 — The board mod  · status: todo
**Depends on:** 3

#### Work Order

**Goal:** `/unit-board` opens a pane showing the production board (or, in a unit worktree, that unit), it stays current while open, and nothing runs or shows while it is closed.

**Spec:**
- **Plugin.** `mods/unit-board/.claude-plugin/plugin.json` (`name: unit-board`, `version: 0.1.0`), `hooks/hooks.json` `{"modules": ["./register.tsx"]}`, `tsconfig.json` as `mods/postmaster/tsconfig.json` (with `.tsx` in `include` and `jsx` settings from the prototype). Local marketplace `mods/.claude-plugin/marketplace.json` listing `unit-board` (and `postmaster`, unchanged in behaviour).
- **Modules.** `hooks/register.tsx` holds only wiring; `hooks/data.ts` (generator call, file read, stat refresh), `hooks/geometry.ts` (strip, ruler, label runs), `hooks/board.tsx` (A), `hooks/unit.tsx` (B). Types from `types/board.d.ts`; plugin state declared as the prototype's `types/index.d.ts` does.
- **Commands.** `session.start` only registers `/unit-board` and, when `$.ui.list()` shows the pane already open (a module reload), restarts the refresh; nothing else.
  - `/unit-board` — run the generator once (`$.process.run([py, board_json.py, "--cwd", cwd])`, `py` = `$HOME/.claude/scripts/lib/py`), read its files, open pane `unit-board` (title `Units`): B for `focus`, else A.
  - `/unit-board <unit>` — B for that unit. `/unit-board close` — close the pane. The command result text is one short line (`Unit board opened.`).
  - In the pane: each unit's `open` button shows B; B has a `board` button back to A.
- **Refresh while open only.** On open start `$.clock.every(10_000)`: `$.fs.stat` each path in `sources` and the board file; if any mtime is newer than the board file, run the generator once, then `$.fs.read` and `$.state` update (a redraw follows the state write). `turn.complete`: when open, the same check; when closed, `next(e)` at once. `ui.close` (any origin, `plugin`, `person`, `unload`): cancel the timer and pass to `next`. A missing file runs the generator once.
- **A (board).** Per production: header (slug, unit count, `as of HH:MM`), then per unit: `● key  Phase n/of  title  open`; the phase strip (█ done, ▓ open, ░ todo, ▒ gated; above 30 phases show counts `44/45 done` instead of shrinking); stage; ETA line (`ETA 18:30  first 17:15 · moved 2× · +1:15 late · fix rounds 1 + 1 running`, or `on its first ETA`, or `gated until …`); the ruler; a workers line (`1 fix running · 2 review idle`). Layout as `mockup-A-board.txt`.
- **B (unit).** Summary; ETA and its trail (`17:15 said 14:42 → 17:45 said 14:48 → 18:30 said 15:03`); ruler; the current phase's steps with times; workers table; phase record rows (`took`, fix rounds as dots, `-1:13 early, said 14:15`). Layout as `mockup-B-unit.txt`.
- **Ruler** (geometry.ts, pure): window from phase start to max(ETA, now) + 10 %; elapsed `━`, earlier ETAs `○`, current ETA `●`, now a red `│`; labels that would overlap are dropped, later mark wins; `now` is the engine clock, labelled with `utc_offset_minutes`.
- **Colours** are theme keys only (`success`, `error`, `warning`, `suggestion`, `inactive`, `subtle`).
- **No band, no status line, no toast** (Invariants).
- **Tests** (`tests/*.test.ts`, the postmaster kit style): mount on `terminal` and `desktop`; `/unit-board` opens A and, with a `focus`, B; `open`/`board` presses switch views; a changed source mtime runs the generator once and redraws; an unchanged one reads nothing; closed → no `fs`, `process` or timer call on `turn.complete` or over a fake-clock minute; `ui.close` cancels the timer; geometry cases for the ruler window, label dropping and the strip's count form.
- **Install.** The user runs once per machine: `/plugin marketplace add ~/.claude/mods` then `/plugin install unit-board` at user scope. The unit director adds that to the change review (`/change_review`) with the live check below; it never edits `settings.json`.
- **Live check** (unit director): load with `command claude --plugin-dir <worktree>/mods/unit-board` in a throwaway tmux session on a private socket (`tmux -L unitboard`), at 170 columns: `/unit-board` from the trunk checkout draws A with real data, from this worktree draws B; close it and confirm with a fake source touch that no generator runs while closed. Capture both panes as text. The phone check (Remote Control, `/unit-board` from the phone) goes to the change review for the user.

**Files:** `mods/unit-board/` — `.claude-plugin/plugin.json`, `hooks/hooks.json`, `hooks/register.tsx`, `hooks/data.ts`, `hooks/geometry.ts`, `hooks/board.tsx`, `hooks/unit.tsx`, `types/index.d.ts`, `tsconfig.json`, `tests/*.test.ts`; `mods/.claude-plugin/marketplace.json`.

**Workers:** 1 writer + 1 tester
- `impl` — every file except `tests/`; hub: `mods/unit-board/types/index.d.ts` (plugin state both sides use).
- `test` — `tests/geometry.test.ts` from the ruler and strip rules above, and `tests/register.test.ts` from the command, refresh and closed-quiet rules, against `types/board.d.ts` fixtures.

**Constraints from prior phases:** the generator CLI, output line `{"files", "focus"}`, file location and JSON contract `mods/unit-board/types/board.d.ts` (Phase "Board data"); times are `{at, text}`; `sources` lists the files to stat.

**Acceptance gate:** `tsc`, `command claude plugin validate --strict mods/unit-board` and `command claude plugin test mods/unit-board` exit 0; the live check's captures match the mockups' layout.

### Phase 5 — Shorter terminal report  · status: todo
**Depends on:** 4

#### Work Order

**Goal:** `/unit:report` and its notifier ticks print a short text report — phase and stage, one ETA line, the workers, the summary — and the two recorder tables only on `/unit:report tables`.

**Spec:**
- The ETA line, printed by `progress_history.py progress` and by `_print_last_recorded`: `ETA 18:30 (first 17:15, +1:15, moved 2×, 1 fix round + 1 running)`; on its first ETA `ETA 16:10 (on its first ETA, 1 fix round)`; gated `gated until Wed 22 Oct 11:47` or `gated by G3: <Waits on>`. From the phase record and chain of Phases "Predictions chain from the stated ETA".
- The clocks table and round table (`_summary_table`, `_phase_section`) print only with a new `progress --tables` flag; `commands/unit/report.md` passes it only for `/unit:report tables`.
- `commands/unit/report.md`: the report's sections become phase and stage, the ETA line, the worker table, the summary sentences, and one line naming `/unit-board` as the place to see the board.
- The vault note keeps the full phase list.

**Files:** `scripts/delegate/progress_history.py`, `scripts/delegate/test_progress_history.py`, `commands/unit/report.md`.

**Workers:** 1 writer + 1 tester
- `impl` — `progress_history.py`, `commands/unit/report.md`; hub: `scripts/delegate/progress_history.py`.
- `test` — `progress` prints the ETA line forms above and no tables; `progress --tables` prints both tables as before.

**Constraints from prior phases:** the phase record (`first_eta`, `moves`, `delta_minutes`, `fix_rounds`) and the gate text come from `phase_table`; the board command is `/unit-board`.

**Acceptance gate:** the Test and Lint lines of Delegation Context green.

### Phase 6 — Accounts view  · status: todo
**Depends on:** 4

#### Work Order

**Goal:** `/accounts-board` opens a pane showing the units/showrunners line, the accounts table with each login's address, and the problems under it, drawn from disk straight to the screen, current while open, with nothing running while closed.

**Spec:**
- **Starts after** two-accounts-unit's `accounts.py status --json --stored` with its login field is promoted to main (the showrunner says when; set by the showrunner 2026-10-16). That JSON is the data source; never parse the text table. Its shape: `accounts` rows with `label`, `tool`, `role`, `weekly_left_percent`, `resets_at`, `credits`, `use_credits`, `read_at`, plus `problems`; it carries no login addresses unless the opt-in login field below is asked for. Copy its exact field types into `mods/unit-board/types/accounts.d.ts` from the merged code.
- **Stored-only mode with logins** (requested by the showrunner from two-accounts-unit, 2026-10-16): `status --json --stored` reads stored readings only — no `claude auth status`, no HTTPS usage call, no `codex app-server`, no `ssh` — and an opt-in login field (flag name from the merged code) adds each row's login address for local on-screen consumers. A plain `status --json` would read any login whose stored reading is over 10 min old live (`_stored_usage`, `accounts.py` 3526–3554). Every run this phase makes, the open run included, is `status --json --stored` with the login field. This phase does not edit `accounts.py`.
- **The mod never opens a credential file** (the showrunner, 2026-10-16: those files are read for nothing beyond what they must be): no read of `~/.claude<N>/.claude.json`, any `auth.json` or any `id_token`. Login addresses come only from the opt-in login field, and are only drawn: never in a command result, `$.ui.log`, a toast or a message.
- **Mod** (`hooks/accounts.tsx`, wiring in `register.tsx`): `session.start` also registers `/accounts-board`, and restarts its refresh when `$.ui.list()` shows the pane open. `/accounts-board` runs `status --json --stored` with the login field once, opens pane `accounts-board` (title `Accounts`), and draws: the units/showrunners line built from the `role` fields; the table (Label, Login, Role, Weekly left, Weekly resets, On credits, Credits left, Read; weekly left under 10 % `error`, under 25 % `warning`); the problems under it. `/accounts-board close` closes it. The command result text is `Accounts opened.` only.
- **Refresh while open only.** `$.clock.every(10_000)` stats `~/.local/state/agent-notes/usage.json`, `~/.local/state/accounts/groups.json` and `~/.claude/config/accounts.json`; on a newer mtime run the same command once and redraw. `usage.json` moves about every 2 min (the `agent-sessions-snapshot` timer), so an open pane runs one stored read per move and none between. `ui.close` cancels the timer; `turn.complete` does nothing for this pane. Closed: no timer, stat or process. Reuse the board's stat-and-run helper in `hooks/data.ts`.
- **Tests** (`tests/accounts.test.ts`): `/accounts-board` opens and draws a fixture with its login field; no `$.fs` call names a `.claude.json` or `auth.json` path; a changed `usage.json` mtime runs the stored read once and an unchanged one runs nothing; closed → nothing over a fake-clock minute; the command result holds no address.
- **Live check:** in the throwaway session, `/accounts-board` draws the table; touching `usage.json` redraws within 10 s; closing stops every stat. Capture the pane to a scratchpad file and confirm it by headings and row count only, never pasting rows into a reply or message.

**Files:** `mods/unit-board/hooks/accounts.tsx`, `hooks/register.tsx`, `types/accounts.d.ts`, `tests/accounts.test.ts`.

**Workers:** 1 writer + 1 tester
- `impl` — `hooks/accounts.tsx`, `types/accounts.d.ts`; hub: `mods/unit-board/hooks/register.tsx` (two wiring lines).
- `test` — `tests/accounts.test.ts` from the Spec and a fixture in the merged JSON's shape.

**Constraints from prior phases:** the mod's layout, `register.tsx` wiring-only rule, the open/close and closed-quiet pattern, the 10 s stat refresh, the `hooks/data.ts` helper and the test kit usage come from Phase "The board mod".

**Acceptance gate:** `tsc`, `command claude plugin validate --strict mods/unit-board` and `command claude plugin test mods/unit-board` exit 0; the live check above.
