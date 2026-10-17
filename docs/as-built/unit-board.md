# Unit board

## What it is

A production runs several `/unit:direct` units at once, and `/unit:report` used to answer "where is this unit" with long recorder tables in the terminal. The unit board gives that answer three ways. Every ETA a unit director states is recorded against the phase it names, and the phase chain predicts later phases from that stated ETA, so a phase's first promise, its moves and its latest promise are all on disk. A Claude Code mod, `mods/unit-board/`, draws those records in a pane the user opens on demand: `/unit-board` shows the production board (every unit's phase strip, ETA, gate, finish and workers) or one unit's detail, and `/accounts-board` shows the accounts table that `accounts.py` reports, logins included, without the addresses ever passing through a model reply. `/unit:report` prints a short report (phase, stage, one ETA line, workers, summary), and adds the recorder's tables only when asked with `tables`.

## How it works

### Data flow

```
director states an ETA ──► progress_history.py eta ──► eta_stated event (runs/<run_id>.jsonl)
                                                              │
                         phase_table.build_plan(plan) ◄───────┘  (chain, gates, PhaseHistory)
                                  │                        │
            progress_history.py progress           board_json.py --cwd <cwd>
            (short report / --tables)              writes <slug>.json, prints {files, focus, watch}
                                                           │
                                       mod: /unit-board ── reads JSON, stats sources while open
                                       mod: /accounts-board ── runs accounts.py status --json --stored --with-login
```

### Recording ETAs

[`progress_history.py`](../../scripts/delegate/progress_history.py) `eta` subcommand:

```
progress_history.py eta --session-dir <dir> --time <t> --basis <basis>
                        [--phase-id <id>] [--earliest <t> --latest <t>]
```

- `<t>` is `HH:MM` (today, user zone) or `YYYY-MM-DDTHH:MM`; `_local_eta_epoch` expands the short form. A past time, a time the local clock skips, a blank basis, `--earliest` without `--latest` (or the reverse), or a range that excludes `--time` is refused.
- The `eta_stated` event carries `eta_at`, `basis`, `eta_phase_id` (the given `--phase-id`, else the active phase's id) and, with a range, `eta_earliest_at`/`eta_latest_at`. No active phase and no `--phase-id` refuses. It prints `ETA recorded: HH:MM <zone>`, and the director states that printed time.
- `basis` is `checkpoint` (the checkpoint notice's next-phase ETA), `eta-command` (`/unit:eta`, `/unit:eta_breakdown`) or `stated` (any other message).
- Who records: `<ProgressContract>` in [`commands/unit/direct.md`](../../commands/unit/direct.md) records every ETA before stating it; [`docs/delegate/phase_end.md`](../../docs/delegate/phase_end.md) step 2 and [`docs/production_format.md`](../../docs/production_format.md) item 3 record the next-phase ETA with `--basis checkpoint` before the notice; [`commands/unit/eta.md`](../../commands/unit/eta.md) and [`commands/unit/eta_breakdown.md`](../../commands/unit/eta_breakdown.md) use `--basis eta-command`, the latter on every answer.

### Chained predictions

[`plan_history.py`](../../scripts/delegate/plan_history.py) holds the plan readers both sides share (`plan_runs`, `plan_phases`, `PlanPhase`, `history_root`, `now_epoch`, `resolve_plan_path`, `eta_band_seconds`, `percent_spread`, `BLOCKED_BY_PATTERN`, `STATE_FILENAME`, `EtaProjectionUnavailable`). [`phase_table.py`](../../scripts/delegate/phase_table.py) builds the chain:

- `_instance_eta_events(runs, instance)` selects a phase instance's `eta_stated` events by `eta_phase_id` (falling back to `phase_id` for older events), within the same run, after the previous start or finish of that phase id and before its next start. An ETA recorded before its phase starts counts for that phase. `_latest_stated_eta` builds on it.
- `PhaseHistory` is the base of the `DonePhase`/`OpenPhase`/`TodoPhase` rows: `first_eta`, `moves` (time changes only; unchanged restatements are recorded but do not count), `eta_trail` (`[(eta_at, stated_at)]`, the real promises), `finished_at`, `delta_minutes`, `fix_done`, `fix_open_passes`, `fix_running` (0 or 1), `fix_rounds` (done plus open), `findings`. History reads only the row's own title family, so a retired task's attempts under a reused phase number are excluded.
- `build_plan(plan_path) -> PhaseRecord`: the open phase ends at its latest future stated ETA; a passed one is advanced to now for scheduling (a phase never ends in the past); with none, the `_reported_eta` projection; with neither, `started + typical`. That end is `OpenPhase.schedule_end`. Todo phases follow back to back, each `typical` long (median of completed phase durations, else the open phase's own stated span) with `gap` (median gap between phases) before each.
- Gates: `_production_gates` reads the production doc's `## Gates` table (`Gate | Waiting | Waits on | Clears when`) for phases carrying `**Blocked by:** G<k>`. `Clears when` starting `cleared` → `NoGate`; a `YYYY-MM-DD HH:MM` (first match, user zone) → `TimedGate(gate, until, waits_on)`, holding the phase's start to that time; no time → `UntimedGate(gate, waits_on)`, which leaves that phase and every later one unpredicted. Outside a production `Blocked by` is ignored. The plan finish is a time or `GateBlockedFinish(gate, waits_on)`.
- `OpenPhase.instance_id` names the instance `build_plan` selected as open.
- `build_run(plan_path, events) -> PhaseRecord` builds the record from one run's own events, for a plan that resolves to no phases (for example after the plan became an as-built doc). Every `phase_finished` is a done row titled from its finish event; a start with no finish is an open row. It goes through the same `_build_phase_record`, `_open_instance`, `_with_history` and `_plan_finish` path as `build_plan`, and the open row matches by instance id, whatever its title.

### The board generator

```
~/.claude/scripts/lib/py ~/.claude/scripts/delegate/board_json.py (--cwd <path> | --production <slug>)
```

[`board_json.py`](../../scripts/delegate/board_json.py) resolves productions, writes each one's board atomically (temp file in the same directory, then `os.replace`) to `${UNIT_BOARD_DIR:-${XDG_STATE_HOME:-~/.local/state}/unit-board}/<slug>.json`, and prints one line `{"files": [...], "focus": <unit key|null>, "watch": [...]}`. Nothing resolved exits 0 with `"files": []`. It logs nothing.

- Resolve (`showrunners.registered_showrunners()` + `production_units.live_unit_table`): `--cwd` inside a unit worktree gives that production and `focus` = the unit; inside a showrunner checkout, its production; anywhere else, every production whose showrunner is running, `focus` null.
- `watch` is the notifier registry dir (`NOTIFIER_STATE_DIR`, default `~/.local/state/notifier`) when it exists, so an open pane notices a production started later.
- The JSON contract is [`mods/unit-board/types/board.d.ts`](../../mods/unit-board/types/board.d.ts), field for field, and [`test_board_json.py`](../../scripts/delegate/test_board_json.py) checks every output key against it. Top level: `production`, `as_of`, `zone`, `utc_offset_minutes`, `units`, `sources`. Every time is `BoardTime {at: epoch s, text}` in the production's zone, `text` `HH:MM` today, else `ddd DD Mon HH:MM`.
- `BoardUnit`: `key` (Unit cell less `-unit`), `plan`, `state` (`running` | `run-finished` | `standing-by`, from the unit's own newest run via `unit_run.local_run`), `phase {id, of, title}` or null, `phases` (`BoardPhase`: `status` `done|open|todo|gated`, `took`, `fix_done`, `fix_running`, `findings {blocker, minor, nit}`, `eta {first, trail, finished, delta_minutes}`, todo `start`/`finish`, `gate` `none|timed|untimed`; a non-`none` gate makes the status `gated`), `finish` (`predicted`/`finished` with `at`, `after_gate` with `gate`/`waits_on`, or `unknown`), `stage`, `eta` (`BoardEta {at, first, trail, moved, delta_minutes, schedule_end, fix_done, fix_running}`) or null, `summary` (newest `progress_reported` text), `steps` (`[state, what, started, took_seconds]` for the open instance's passes and activities), `workers` (`slot`, `role`, `state`, `model`, `doing`).
- `stage`: the open pass as `Build`/`Tests`/`Review`/`Repair round <n>`, else the open activity label (`units_table.open_step`), else `Run done`, `Waiting on G<k>` or `Standing by`.
- `state` comes from `unit_run.local_run`, which reads each run file only up to its `run_started` line (every run file holds one, on line 1) and skips lines without `"run_started"` before parsing.
- The record is `build_plan(plan)`; when that has no phases, `build_run(plan, events)` from the found run's events.
- `workers` come from the run's session dir: `mesh_roster.json` (role, status; `done` shows as `idle`, `ended` stays), `impl_worker_<slot>.json` (slot, family/model) and the agent's newest `board.log` line as `doing`, less a leading `launcher: ` and a trailing `; summary at …` or `; findings in …`, stripped and cut to 80 characters.
- `eta.at`/`first`/`trail` are stated promises only; `schedule_end` is the chain's end and never a trail entry.
- `sources` lists existing paths only: each unit's run `.jsonl`, `mesh_roster.json`, `board.log`, every `impl_worker_<slot>.json`, the plan doc, every `plan_runs` `.jsonl`, the production doc, plus each session dir, the run roots and the history `runs/` dir (or the history root while `runs/` is missing), so a run that starts later moves a watched mtime.

### The mod

`mods/unit-board/` is a Claude Code mod (TypeScript, JSX with `h`, no DOM, no Node; every engine call goes through `$`). [`hooks/hooks.json`](../../mods/unit-board/hooks/hooks.json) loads [`register.tsx`](../../mods/unit-board/hooks/register.tsx); [`mods/.claude-plugin/marketplace.json`](../../mods/.claude-plugin/marketplace.json) (`nate-local`) lists it beside `postmaster`. The user installs it per machine with `/plugin marketplace add ~/.claude/mods` and `/plugin install unit-board`.

| File | Role |
| --- | --- |
| `hooks/register.tsx` | Every direct engine call: commands, pane lifecycle, timers, state, render hooks. |
| `hooks/data.ts` | `refreshIfChanged`, `snapshotPaths`, `loadBoard`, `loadAccounts`, `resolveUserZone` and their narrow service interfaces (`BoardLoader`, `AccountsLoader`, `UserZoneSettings`, `RefreshFiles`). |
| `hooks/geometry.ts` | Pure layout: `stripCells`, `rulerCells`, `labelRuns`, `runsOf`, `clockText`, `signedMinutes`, `duration`. |
| `hooks/board.tsx` | `BoardView`, `RulerView` and the shared text helpers `etaText`, `gateText`, `finishText`, `workersText`. |
| `hooks/unit.tsx` | `UnitView`: one unit's detail. |
| `hooks/accounts.tsx` | `AccountsView`, `accountDisplay` and the display types. |
| `types/board.d.ts`, `types/accounts.d.ts` | Wire contracts of the generator and of `accounts.py`. |
| `types/index.d.ts` | `BoardSnapshot`, `PaneState`, `AccountsPaneState`, the `PluginState` keys. |
| `tests/` | `kit.ts` fixtures; `geometry`, `register` and `accounts` tests. |

**Commands.**

- `/unit-board [<unit>|close]` opens pane `unit-board` (title `Units`, closes on Escape), stores `loading`, runs the generator once with `--cwd <session cwd>`, reads each listed file and stores the unit view for `<unit>` or `focus`, else the board view; it replies `Unit board opened.` `open` buttons on the board and the `board` button on a unit switch views. `/unit-board close` replies `Unit board closed.`
- `/accounts-board [close]` resolves the user zone, opens pane `accounts-board` (title `Accounts`), stores `loading`, runs `[~/.claude/scripts/lib/py, ~/.claude/scripts/accounts/accounts.py, "status", "--json", "--stored", "--with-login"]` once and replies `Accounts opened.`; `close` replies `Accounts closed.` Every open and refresh runs exactly that argv; the text table is never parsed.
- `session.start` only registers both commands and, if `$.ui.panes()` shows a pane already open (a reload), restarts that pane's refresh.

**State.** Plugin state `unit-board` holds two keys, each a record keyed by pane id; a missing entry is a closed pane.

- `panes`: `PaneState<BoardData>` = `loading` | `board {data, now}` | `unit {data, production, unit, now}` | `failed {reason}`. `data` is a `BoardSnapshot {files, watch, loadedAt, boards, focus}`.
- `accountsPanes`: `AccountsPaneState<StoredAccountsStatus>` = `loading` | `accounts {data, zone}` | `failed {reason}`.

Timers stay module-level and are never persisted: `OpenPane {timer, cwd, busy, paths, selection}` (`Selection = {kind:'board'} | {kind:'unit'; production; unit}`) and `OpenAccountsPane {timer, busy, paths, zone}`. Paths and selection survive a failed refresh. An `accountsLifecycle` counter, bumped on `session.start`, open and `ui.close`, cancels an accounts load or zone lookup that a close overtook.

**Refresh on change.** Both panes share:

```ts
refreshIfChanged<T>(files: RefreshFiles, paths: readonly RefreshPaths[],
  generator: () => Promise<T>, set: (fresh: T) => Promise<void>, isOpen: () => boolean): Promise<boolean>

type RefreshPaths =
  | { kind: 'board'; board: string; sources: string[] }       // sources vs the board file's mtime
  | { kind: 'discovery'; sources: string[]; loadedAt: number } // sources vs the load time
```

It stats each source; one newer than its reference (or a failed stat) runs the generator once and stores the result. `isOpen` is checked before every stat, before the generator and before the set. The board ticks on a `$.clock.every(10_000)` timer and on `turn.complete`; an unchanged board tick only moves `now`, so the ruler's now mark advances without a process. The accounts pane ticks on its own 10 s timer, with a `busy` guard, and ignores `turn.complete`. `loadAccounts` watches `~/.local/state/agent-notes/usage.json`, `~/.local/state/accounts/groups.json`, `~/.claude/config/accounts.json` and `~/.local/state/accounts/role_map_cache.json` as one `discovery` path; a missing input is watched through its nearest existing parent directory. `ui.close` (any origin) cancels the pane's timer and drops its state.

**Views.** Views draw the `as_of` snapshot and never recompute an ETA. Colours are theme keys only (`success`, `error`, `warning`, `suggestion`, `inactive`, `subtle`).

- Board: per production a heading with unit count and `as of`. Units sort running, then standing by, then run done. A finished unit is one line: state dot, key, `Run done` and its finish, and the `open` button. Any other unit has a header line (state dot, key, phase `id/of` and title, or the stage when no phase is running, and `open`), then the phase strip (`█` done, `▓` open, `░` todo, `▒` gated) with the stage when a phase is running, the ETA line (`ETA HH:MM  first … · moved N× · +H:MM late · fix rounds N + 1 running`, or `on its first ETA`), the gate line, `done by`/`after G<k>`, the ruler and a workers line grouped by role and state. The finish shows only while a phase is running or a todo or gated phase remains. The workers line is left out when every worker has ended, and `workersText` skips empty parts.
- Ruler (`rulerCells`): from the start of the open row with the current phase id to `max(promise, now, start + 1s)` plus 10 %; `○` for each trail entry, `●` for `eta.at`, `│` for now; labels via `labelRuns`, where a later mark displaces whole earlier labels.
- Unit: heading with stage, title, summary, ETA, gate, finish, ruler, the trail as `HH:MM said HH:MM → …`, the strip, `This phase` steps, one row per worker with slot, role and state padded to the widest entry, and a newest-first phase record (took, fix marks `●`/`○`, `+H:MM late, said HH:MM`), which reads `no phases recorded` when empty. A block with nothing in it is not drawn.
- Accounts: `units: <labels>, showrunners: <labels>` from `roles` (units match `units` and `unit directors`; none reads `unassigned`), then Label, Login, Roles, Weekly left, Weekly resets, On credits, Credits left, Read, then Problems. `accountDisplay` maps the wire shape into `AccountReading<T>` (`available`/`unavailable`) and `CreditBalance` (`finite`/`unlimited`/`unavailable`), so a null reading shows `unavailable`, never 0. Dates format with `Intl.DateTimeFormat` in the resolved zone.
- `resolveUserZone(calls, isActive): Promise<ZoneLookup>` follows [`scripts/lib/user_zone.py`](../../scripts/lib/user_zone.py): `USER_ZONE`, then the `zone=` line of `USER_ZONE_CONF` or `~/.claude/config/user_zone.conf`, then `America/New_York`, validated through `Intl.DateTimeFormat`; it returns `{kind: 'cancelled'}` once the lifecycle moves on.

### The short report

`progress_history.py progress` (and `_print_last_recorded` between windows) prints by default the phase heading, one ETA line from `_phase_eta_line(session_dir, phase_number)`, the stage line and one `- **Agent N**` line per worker. `--tables` adds the clocks table (`_summary_table`) and the round table with its `Earlier:` line; verification gate lines print in both forms.

- ETA line forms: `ETA HH:MM (first HH:MM, +H:MM, moved N×, …)`, `ETA HH:MM (on its first ETA, …)`, a leading `passed` when an open phase is overdue, fix rounds as `N fix rounds`, `+ 1 running` or `1 fix round running`, `gated until Wed 22 Oct 11:47`, `gated by <gate>: <Waits on>`, and `ETA unavailable` when no plan, state, row or promise exists. A promise on another local day shows weekday and date. The line reads only the reported phase's own row and gate.
- Worker lines come from `_current_worker_windows` / `_current_worker_cells`: per slot, the live window, else the most recent finished one, independent of the director's own activity.
- In the clocks table Project ETA is the chain's finish (`_project_chain_eta`), low/high shifted by the open phase's band; under an untimed gate it reads `after G<k>` with `—` for low/high.
- The legacy (non-dual) layout keeps its old form plus one ETA line after the phase line. The vault phase note keeps the full phase list.
- [`commands/unit/report.md`](../../commands/unit/report.md): `/unit:report [tables|on|off]` presents phase and stage, ETA line, worker table, summary sentences, then `See /unit-board for the board.`; `tables` passes `--tables` and adds both tables after the ETA line. `<DelegationResultFormat>` in [`commands/unit/direct.md`](../../commands/unit/direct.md) closes every delegation result and launch with this short report.

## Invariants

- **Nothing shows by itself.** A pane opens only from its slash command or a press inside an open pane, and closes from its command, Escape or its close mark. No pane at `session.start`, no `AbovePrompt` band, no status line, no toast.
- **A closed pane does no work.** With a pane closed there is no timer, no `$.fs` call and no `$.process.run` for it; with no board pane open, `turn.complete` passes straight to `next`, and it never refreshes accounts. While open, refresh is `$.fs.stat` of existing files, and a process starts only on open or when a stat shows a change.
- **No new long-running process.** No daemon, service or timer job exists outside the mod; the generator and `accounts.py` are one-shot runs.
- **The mod never opens a credential file.** It never reads `.claude.json`, `auth.json` or any `id_token`; logins arrive only in the `login` field of `accounts.py --stored --with-login`.
- **Login addresses are only drawn.** They never appear in a command reply, log, toast or message; a failed accounts run shows a fixed reason, never process output.
- **User-facing times use the user zone.** Python writes them with `astimezone(user_zone())`; the mod resolves the zone in `scripts/lib/user_zone.py`'s order; board times use the production's registered zone. `scripts/lib/test_user_zone` scans every script.
- **The ETA is the latest stated promise**, never the schedule end an overdue phase is advanced to. `eta.at`, `first` and `trail` hold promises only; `schedule_end` is never a trail entry; the ruler's ETA mark is `eta.at`.
- **Readers place an ETA by `eta_phase_id`**, never `phase_id`, which names the phase active when it was recorded.
- **The mod never computes ETAs or predictions**; it draws the board JSON. The recorder never writes the board file; only `board_json.py` does.
- **`board.d.ts` is the generator's contract**, and every output key is tested against it.
- **Imports stay one-way.** `phase_table` imports `plan_history`, never `progress_history`; `progress_history` imports `phase_table` only inside function bodies (`_project_chain_eta`, `_phase_eta_line`). basedpyright's import-cycle check is file-level with no line anchor, so a cycle must be removed, not suppressed.
- **The board and accounts panes are independent**: separate state keys, timers and lifecycles, so either opens and closes without touching the other. Engine calls stay in `register.tsx`.
- `settings.json` is never edited to install the mod. `.gitignore` allows `mods/**` and excludes `mods/**/.claude-plugin/types/`.

## Calibration and gotchas

- Refresh interval: 10 s for both panes (`$.clock.every(10_000)`), plus `turn.complete` for the board.
- Phase strip: above 30 phases, or more phases than the room, it shows `N/M done` instead of cells.
- Ruler horizon: 10 % beyond the later of the promise and now; board ruler width is `bodyColumns - 4`, unit `bodyColumns - 2`, minimum 8.
- `doing` is cut to 80 characters, after the `launcher: ` prefix and the summary or findings path are removed.
- `local_run` stops at the `run_started` line: parsing every line of every run file once per unit took 35 s for three productions, past the mod's 30 s process limit. Reading to that line brings it to about 4.5 s.
- Weekly left under 10 % is `error`, under 25 % `warning`.
- `HH:MM` for `--time`, `--earliest`, `--latest` means today only; a later-day ETA needs the full date.
- A phase record's `first_eta` comes from `_instance_eta_events`, so a checkpoint-notice ETA recorded before the phase starts counts as its first.
- `build_plan` can select an older resumed phase as open, so the newest `phase_started` is the wrong instance for `steps` and `stage`; both follow `OpenPhase.instance_id`.
- `phase_table show --json` drops gates and turns `GateBlockedFinish` into null; the board builds from `build_plan`'s record instead.
- Taking worker cells from the round table's last row blanks them once the director opens an activity; reading the next gated phase instead of the reported one gives the wrong ETA line.
- A failed stat counts as a change: a `failed` board pane retries each tick while a source stays newer than the board file, and recovers on the next good run.
- A close during a pending `session.start` load or zone lookup must not restart the accounts timer; the lifecycle counter guards it.
- The mod runs the generator and `accounts.py` installed under `~/.claude/scripts`, not a worktree's copy, so a live check of the mod proves only promoted code; a contract change can only be checked live after it reaches main.
- `accounts.py status` with no flags prints login addresses. Never run it to look up an account label; take the label from the folder name (`~/.claude2` → `claude2`).
- A fresh checkout's `tsc` reports `TS2307` for `claude-code` until a live session loads the folder and the engine writes `mods/unit-board/.claude-plugin/types/`.
- Tests patch `plan_history.now_epoch` for the clock.

## Why

The recorder already held everything the board needs, but as tables written for a terminal, and a unit's ETA lived only in the director's prose. Recording each stated ETA, with the phase it names and its basis, turns promises into data: the board can show the first promise, every move and the latest one, and the chain can predict later phases from what the director actually said rather than from a projection. Recording before stating, and stating the recorder's printed time, keeps the spoken ETA and the recorded one identical.

The ETA shown is the latest promise because a passed promise advanced to now would read as on time while the phase runs late. The chain still advances it for scheduling, since later phases cannot start in the past; the two values are kept apart (`eta.at` versus `schedule_end`) so neither leaks into the other.

The mod draws and Python computes. The chain, gates and history live in `phase_table`, which is stdlib Python under `unittest` and basedpyright; a generator that writes one JSON file keeps a single source of predictions, a typed contract tested on both sides, and a view layer with no logic to drift. `state` comes from the unit's own newest run instead of `unit_lookup.run_state`, which reads every history run file per unit, and `local_run` reads each file only to its `run_started` line so the board opens inside the process limit. `build_run` keeps a unit's record on the board after its plan turns into an as-built doc and loses its phase headings.

The panes open only on demand because the user asked for it, and a closed pane costs nothing for the same reason a background watcher was ruled out: no session should pay for a view nobody is looking at. While open, stat-based refresh is cheap: the sources are files the run already writes, so an unchanged tick is a handful of stats and a redraw, and the generator runs only after something changed. The `discovery` paths exist so a production or input that appears later still triggers a refresh without a scan.

The accounts view draws `accounts.py`'s stored status straight to the screen so login addresses reach the user without passing through a model reply, a log or a message. It reads stored values (`--stored`) rather than probing accounts, and reruns only when one of its four input files changes, so an open pane spends no quota and no network.

The terminal report got shorter because the tables answered questions most ticks do not ask; the board carries the visual view, and `tables` keeps the full recorder output one word away.
