# Unit board

## What it is

A production runs several `/unit:direct` units at once, and `/unit:report` used to answer "where is this unit" with long recorder tables in the terminal. The unit board gives that answer three ways. Every ETA a unit director states is recorded against the phase it names, and the phase chain predicts later phases from that stated ETA, so a phase's first promise, its moves and its latest promise are all on disk. A Claude Code mod, `mods/unit-board/`, draws those records in a pane the user opens on demand: `/showrunner:unit_board` shows the board of the showrunner's production and every production below it (every unit's phase strip, ETA, gate, finish and workers) or one unit's detail, and `/accounts-board` shows the accounts table that `accounts.py` reports from stored readings, with no login addresses. `/unit:report` prints a short report (phase, stage, one ETA line, the summary table, workers, summary sentences), and adds the recorder's round table only when asked with `tables`.

## How it works

### Data flow

```
director states an ETA ──► progress_history.py eta ──► eta_stated event (runs/<run_id>.jsonl)
                                                              │
                         phase_table.build_plan(plan) ◄───────┘  (chain, gates, PhaseHistory)
                                  │                        │
            progress_history.py progress           board_json.py --cwd <cwd> --session <id>
            (short report / --tables)              writes <slug>.json, prints {files, watch}
                                                           │
                                       mod: /showrunner:unit_board ── reads JSON, stats sources while open
                                       mod: /accounts-board ── runs accounts.py status --json --stored
```

### Recording ETAs

[`progress_history.py`](../../scripts/direct/progress_history.py) `eta` subcommand:

```
progress_history.py eta --session-dir <dir> --time <t> --basis <basis>
                        [--phase-id <id>] [--earliest <t> --latest <t>]
```

- `<t>` is `HH:MM` (today, user zone) or `YYYY-MM-DDTHH:MM`; `_local_eta_epoch` expands the short form. A past time, a time the local clock skips, a blank basis, `--earliest` without `--latest` (or the reverse), or a range that excludes `--time` is refused.
- The `eta_stated` event carries `eta_at`, `basis`, `eta_phase_id` (the given `--phase-id`, else the active phase's id) and, with a range, `eta_earliest_at`/`eta_latest_at`. No active phase and no `--phase-id` refuses. It prints `ETA recorded: HH:MM <zone>`, and the director states that printed time.
- `basis` is `checkpoint` (the checkpoint notice's next-phase ETA), `eta-command` (`/unit:eta`, `/unit:eta_breakdown`) or `stated` (any other message).
- Who records: `<ProgressContract>` in [`commands/unit/direct.md`](../../commands/unit/direct.md) records every ETA before stating it; [`docs/direct/phase_end.md`](../../docs/direct/phase_end.md) step 2 and [`docs/production_format.md`](../../docs/production_format.md) item 3 record the next-phase ETA with `--basis checkpoint` before the notice; [`commands/unit/eta.md`](../../commands/unit/eta.md) and [`commands/unit/eta_breakdown.md`](../../commands/unit/eta_breakdown.md) use `--basis eta-command`, the latter on every answer.

### Chained predictions

[`plan_history.py`](../../scripts/direct/plan_history.py) holds the plan readers both sides share (`plan_runs`, `plan_phases`, `PlanPhase`, `history_root`, `now_epoch`, `resolve_plan_path`, `eta_band_seconds`, `percent_spread`, `BLOCKED_BY_PATTERN`, `STATE_FILENAME`, `EtaProjectionUnavailable`). [`phase_table.py`](../../scripts/direct/phase_table.py) builds the chain:

- `_instance_eta_events(runs, instance)` selects a phase instance's `eta_stated` events by `eta_phase_id` (falling back to `phase_id` for older events), within the same run, after the previous start or finish of that phase id and before its next start. An ETA recorded before its phase starts counts for that phase. `_latest_stated_eta` builds on it.
- `PhaseHistory` is the base of the `DonePhase`/`OpenPhase`/`TodoPhase` rows: `first_eta`, `moves` (time changes only; unchanged restatements are recorded but do not count), `eta_trail` (`[(eta_at, stated_at)]`, the real promises), `finished_at`, `delta_minutes`, `fix_done`, `fix_open_passes`, `fix_running` (0 or 1), `fix_rounds` (done plus open), `findings`. History reads only the row's own title family, so a retired task's attempts under a reused phase number are excluded.
- `build_plan(plan_path) -> PhaseRecord`: the open phase ends at its latest future stated ETA; a passed one is advanced to now for scheduling (a phase never ends in the past); with none, the `_reported_eta` projection; with neither, `started + typical`. That end is `OpenPhase.schedule_end`. Todo phases follow back to back, each `typical` long (median of completed phase durations, else the open phase's own stated span) with `gap` (median gap between phases) before each.
- Gates: `_production_gates` reads the production doc's `## Gates` table (`Gate | Waiting | Waits on | Clears when`) for phases carrying `**Blocked by:** G<k>`. `Clears when` starting `cleared` → `NoGate`; a `YYYY-MM-DD HH:MM` (first match, user zone) → `TimedGate(gate, until, waits_on)`, holding the phase's start to that time; no time → `UntimedGate(gate, waits_on)`, which leaves that phase and every later one unpredicted. Outside a production `Blocked by` is ignored. The plan finish is a time or `GateBlockedFinish(gate, waits_on)`.
- `OpenPhase.instance_id` names the instance `build_plan` selected as open.
- `build_run(plan_path, events) -> PhaseRecord` builds the record from one run's own events, for a plan that resolves to no phases (for example after the plan became an as-built doc). Every `phase_finished` is a done row titled from its finish event; a start with no finish is an open row. It goes through the same `_build_phase_record`, `_open_instance`, `_with_history` and `_plan_finish` path as `build_plan`, and the open row matches by instance id, whatever its title.

### The board generator

```
~/.claude/scripts/lib/py ~/.claude/scripts/direct/board_json.py (--cwd <path> [--session <id>] | --production <slug>)
```

[`board_json.py`](../../scripts/direct/board_json.py) resolves productions, writes each one's board atomically (temp file in the same directory, then `os.replace`) to `${UNIT_BOARD_DIR:-${XDG_STATE_HOME:-~/.local/state}/unit-board}/<slug>.json`, and prints one line `{"files": [...], "watch": [...]}`. Nothing resolved exits 0 with `"files": []`. It logs nothing.

- Resolve (`showrunners.registered_showrunners()`): a `--session <id>` whose update timer `showrunners.production_of` finds gives the registered production with that doc (paths compared resolved); failing that, `--cwd` inside a showrunner checkout gives that production. Either is followed by each production in `showrunners.descendants_of(slug)` (nearest level first), each only when registered. `--production <slug>` gives the same list. Any other session and `--cwd`, a unit worktree included, gives no files.
- `watch` holds the notifier registry dir (`NOTIFIER_STATE_DIR`, default `~/.local/state/notifier`), so an open pane notices a production started later. It also holds `SHOWRUNNER_STATE_DIR` and `SHOWRUNNER_STATE_DIR/<slug>` for every registered slug: `showrunners.attach` and `detach` write or remove `<child>/parent`, which moves that dir's mtime. Existing paths only; registration creates `<slug>/` before the update timer, so the dir exists before any attach.
- The JSON contract is [`mods/unit-board/types/board.d.ts`](../../mods/unit-board/types/board.d.ts), field for field, and [`test_board_json.py`](../../scripts/direct/test_board_json.py) checks every output key against it. Top level: `production`, `as_of`, `zone`, `utc_offset_minutes`, `units`, `sources`. Every time is `BoardTime {at: epoch s, text}` in the production's zone, `text` `HH:MM` today, else `ddd DD Mon HH:MM`.
- `BoardUnit`: `key` (Unit cell less `-unit`), `plan`, `state` (`running` | `run-finished` | `standing-by`, from the unit's own newest run via `unit_run.local_run`), `phase` (tagged by `kind`: `plan {id, of, title}`, or `ticket {id, title, merged}` when the plan holds a `> **Standing:**` line; one read-only ledger read gives `merged`, the unit's `merged` tickets, and `id`, its newest `assigned` or `checkpointed` ticket (the current phase is then the open `phase_table.build_run` row whose phase or leading title word is that id, so a ticket that a unit rename moved still shows under its old prefix), else the title's leading `<unit>-<n>`, else the phase id; `title` drops that leading id) or null, `phases` (`BoardPhase`: `status` `done|open|todo|gated`, `took`, `fix_done`, `fix_running`, `findings {blocker, minor, nit}`, `eta {first, trail, finished, delta_minutes}`, todo `start`/`finish`, `gate` `none|timed|untimed`; a non-`none` gate makes the status `gated`), `finish` (`predicted`/`finished` with `at`, `after_gate` with `gate`/`waits_on`, or `unknown`), `stage`, `eta` (`BoardEta {at, first, trail, moved, delta_minutes, schedule_end, fix_done, fix_running}`) or null, `summary` (newest `progress_reported` text), `steps` (`[state, what, started, took_seconds]` for the open instance's passes and activities), `workers` (`slot`, `role`, `state`, `model`, `doing`).
- `stage`, in order:
  - the open pass as `Build`/`Tests`/`Review`/`Repair round <n>`;
  - a `finding_batch_dispatched` not yet landed in the open phase instance, as `Repair round <dispatched batches>`;
  - the open activity label (`unit_table_cells.open_step`);
  - `Phase <id> running` while a phase is open (`Ticket <id> running` on a standing plan);
  - otherwise `Run done`, `Waiting on G<k>` or `Standing by`.

  `Standing by` therefore means no phase is open.
- `state` comes from `unit_run.local_run`, which reads each run file only up to its `run_started` line (every run file holds one, on line 1) and skips lines without `"run_started"` before parsing.
- The record is built from the found run's own plan (`project_plan_doc`, else `plan_doc` resolved against the worktree), falling back to the Plan cell's plan.
  - It is `build_plan` on that plan; when that has no phases, `build_run(plan, events)` from the run's events.
  - A run on another plan than its production row (tool-based-ui `demo`) shows its real phase.
  - `plan` is the Plan cell, and is empty when that file no longer exists.
- `workers` come from the run's session dir: `mesh_roster.json` (role, status; `done` shows as `idle`, `ended` stays), `impl_worker_<slot>.json` (slot, family/model) and the slot's newest `board.log` line as `doing`.
  - Slots drop the worktree's mesh prefix.
  - Roster entries that become one slot merge into the entry its `impl_worker_<slot>.json` names, else the last in roster order. Roster `revision` values are per-entry counters, never compared.
  - `doing` is less a leading `launcher: ` and a trailing `; summary at …` or `; findings in …`, stripped and cut to 80 characters.
- `eta.at`/`first`/`trail` are stated promises only; `schedule_end` is the chain's end and never a trail entry.
- `sources` lists existing paths only: each unit's run `.jsonl`, `mesh_roster.json`, `board.log`, every `impl_worker_<slot>.json`, the plan doc, every `plan_runs` `.jsonl`, a standing unit's style ledger, the production doc, plus each session dir, the run roots and the history `runs/` dir (or the history root while `runs/` is missing), so a run that starts later moves a watched mtime.

### The mod

`mods/unit-board/` is a Claude Code mod (TypeScript, JSX with `h`, no DOM, no Node; every engine call goes through `$`). [`hooks/hooks.json`](../../mods/unit-board/hooks/hooks.json) loads [`register.tsx`](../../mods/unit-board/hooks/register.tsx); [`mods/.claude-plugin/marketplace.json`](../../mods/.claude-plugin/marketplace.json) (`nate-local`) lists it beside `postmaster`. Without an install, a session loads it only with `--plugin-dir ~/.claude/mods/unit-board`. To make it available in every session on a machine, run `claude plugin marketplace add ~/.claude/mods` and then `claude plugin install unit-board@nate-local`. Together these add `nate-local` to `extraKnownMarketplaces` and `unit-board@nate-local` to `enabledPlugins` in `~/.claude/settings.json`.

| File | Role |
| --- | --- |
| `hooks/register.tsx` | Every direct engine call: commands, pane lifecycle, timers, state, render hooks. |
| `hooks/data.ts` | `refreshIfChanged`, `snapshotPaths`, `loadBoard`, `loadAccounts`, `resolveUserZone` and their narrow service interfaces (`BoardLoader`, `AccountsLoader`, `UserZoneSettings`, `RefreshFiles`). |
| `hooks/geometry.ts` | Pure layout: `stripCells`, `rulerCells`, `runsOf`, `clockText`, `shortTime`, `elapsedText`, `signedMinutes`, `duration`, plus the text fitters:

- `cellWidth` counts terminal cells: wide and emoji characters 2, combining marks 0.
- `wrappedLines(text, columns, continuationIndent)` wraps at spaces to `columns` cells, never cuts.
- `fitText` flattens line breaks and tabs to one space, then cuts with `…` (the detail view's step labels only).
- `doneText(keys, width, label)` gives `<label>: a, b, +N more`.
- `overdueText` gives ` · overdue <elapsedText>`; `elapsedText` gives `Nm`, `Nh Mm` or `Nd Mh`. |
| `hooks/board.tsx` | `BoardView`, `RulerView` (the detail view's ruler), `healthTone`, and the detail view's text helpers `etaText`, `gateText`, `finishText`. |
| `hooks/unit.tsx` | `UnitView`: one unit's detail. |
| `hooks/accounts.tsx` | `AccountsView`, `accountDisplay`, the shared accounts table and its forms, and the display types. It uses the line helpers `span`, `wrapLine` and `pairedLines` that `board.tsx` exports. |
| `types/board.d.ts`, `types/accounts.d.ts` | Wire contracts of the generator and of `accounts.py`. |
| `types/index.d.ts` | `BoardSnapshot`, `PaneState`, `AccountsPaneState`, the `PluginState` keys. |
| `tests/` | `kit.ts` fixtures; `geometry`, `register` and `accounts` tests. |

**Commands.**

- `/showrunner:unit_board [<unit>|close]` opens pane `unit-board` (title `Units`, closes on Escape), stores `loading`, runs the generator once with `--cwd <session cwd> --session <$.session.id()>` (the open pane keeps both, so every refresh passes them), reads each listed file and stores the unit view for `<unit>` (the first board holding that key), else the board view. With no files the pane shows `Run /showrunner:unit_board in a showrunner session.`; it replies `Unit board opened.` Pressing a unit name on the board and the `board` button on a unit switch views. `close` replies `Unit board closed.`
- A mod command name allows only letters, digits, `_` and `-`, so [`commands/showrunner/unit_board.md`](../../commands/showrunner/unit_board.md) gives the name. The mod answers it with a `command.run` hook on `{ command: 'showrunner:unit_board' }` that returns `{ text }` and never calls `next`, so no model turn starts. In a session without the mod, the file's body makes the model reply one line naming `unit-board@nate-local`.
- `/accounts-board [close]` resolves the user zone, opens pane `accounts-board` (title `Accounts`), stores `loading`, runs `[~/.claude/scripts/lib/py, ~/.claude/scripts/accounts/accounts.py, "status", "--json", "--stored"]` once and replies `Accounts opened.`; `close` replies `Accounts closed.` Every open and refresh runs exactly that argv; the text table is never parsed.
- `session.start` only registers `accounts-board` and, if `$.ui.panes()` shows a pane already open (a reload), restarts that pane's refresh.

**State.** Plugin state `unit-board` holds two keys, each a record keyed by pane id; a missing entry is a closed pane.

- `panes`: `PaneState<BoardData>` = `loading` | `board {data, now}` | `unit {data, production, unit, now}` | `failed {reason}`. `data` is a `BoardSnapshot {files, watch, loadedAt, boards}`.
- `accountsPanes`: `AccountsPaneState<StoredAccountsStatus>` = `loading` | `accounts {data, zone, now}` | `failed {reason}`. `now` (seconds) is set on each load, and each unchanged 10 s tick sets a fresh `now` with no process run.

Timers stay module-level and are never persisted: `OpenPane {timer, cwd, busy, paths, selection}` (`Selection = {kind:'board'} | {kind:'unit'; production; unit}`) and `OpenAccountsPane {timer, busy, paths, zone}`. Paths and selection survive a failed refresh. An `accountsLifecycle` counter, bumped on `session.start`, open and `ui.close`, cancels an accounts load or zone lookup that a close overtook.

**Refresh on change.** Both panes share:

```ts
refreshIfChanged<T>(files: RefreshFiles, paths: readonly RefreshPaths[],
  generator: () => Promise<T>, set: (fresh: T) => Promise<void>, isOpen: () => boolean): Promise<boolean>

type RefreshPaths =
  | { kind: 'board'; board: string; sources: string[] }       // sources vs the board file's mtime
  | { kind: 'discovery'; sources: string[]; loadedAt: number } // sources vs the load time
```

It stats each source; one newer than its reference (or a failed stat) runs the generator once and stores the result. `isOpen` is checked before every stat, before the generator and before the set. The board ticks on a `$.clock.every(10_000)` timer and on `turn.complete`; an unchanged board tick only moves `now`, so `in 2h 16m`/`late …` and the detail ruler's now mark advance without a process. The accounts pane ticks on its own 10 s timer, with a `busy` guard, and ignores `turn.complete`. `loadAccounts` watches `~/.local/state/agent-notes/usage.json`, `~/.local/state/accounts/groups.json`, `~/.claude/config/accounts.json` and `~/.local/state/accounts/role_map_cache.json` as one `discovery` path; a missing input is watched through its nearest existing parent directory. `ui.close` (any origin) cancels the pane's timer and drops its state.

**Views.** Views draw the `as_of` snapshot and never recompute an ETA (`in …`/`late …` is display arithmetic on recorded times). Colours are theme keys only. Facts draw in the default colour; `inactive` is the only grey, for labels (`workers`, `as of`), separators and history. There is no `subtle` and no `dimColor`: the user found the dim grey hard to read.

- Board, per production:
  - A header: the production in bold; on the right `N units · K late · as of HH:MM`, with `K late` (in `error`) only when K > 0.
  - Every unit, sorted by `key` (`localeCompare`, `sensitivity: 'base'`), one blank row apart, the first one too. No `Done:` or `Standing by:` fold lines and no `open` label: the unit name (bold, a plain `Button`) opens the unit.
  - The dot is `healthTone`: `error` when the ETA or the predicted run end is past; else `warning` when a phase is gated or an open phase has no ETA; else `suggestion` running, `success` run finished, `inactive` otherwise.
  - A unit with an open phase:
    - line 1: `● <key>  Phase <id>  <title>` (no `/<of>`), or `Ticket <id> · <n> merged  <title>`;
    - line 2: the strip, `<done>/<total>` (total = `phases.length`), the stage (left out when it is `Phase|Ticket <id> running`), ` · fix rounds <n>[ + 1 running]` only when `n > 0` (`warning` at 3 or more); right-aligned `ETA <time> · in <elapsed>`, `ETA <time> · late <elapsed>` in `error`, or `no ETA` in `warning`;
    - the gate line `Phase <id> waits on <gate>[ until <time>]: <waits_on>` in `warning`;
    - line 3: `workers` and counts by state (running `suggestion`, idle, paused `warning`, then others by name); right-aligned `run ends <time>` (`error` with ` · late …` when past), `run ended <time>` or `run ends after <gate>`.
  - A unit with no open phase is one line: `● <key>  <stage> · <done>/<total> · run ends|ended <time>`.
  - The strip (`stripCells`) is one cell per phase up to 20; above 20 it is 20 cells, cell `i` covering phases `[⌊i·n/20⌋, ⌊(i+1)·n/20⌋)` and showing open, else gated, else todo, else done (`█` done `success`, `▓` open `suggestion`, `░` todo `inactive`, `▒` gated `warning`).
  - Times (`shortTime`, from `at` and `utc_offset_minutes`): same local day `18:00`, within six days `Sat 18:41`, else `24 Oct`.
  - Nothing is cut: lines wrap (titles indented 2 under line 1). A right part that does not fit beside its left part with 2 spaces goes on its own row, indented 2. No row is wider than the pane.
  - No ruler: the detail view keeps it and the full ETA history.
- `BoardView(elements, columns, bodyRows, …)` never draws past `bodyRows`, counting wrapped rows at their real height. First it drops line 3 from the bottom unit up, then the gaps, then whole units from the bottom, named in one `+N more: …` line.
- Ruler (`rulerCells`): from the start of the open row with the current phase id to `max(promise, now, start + 1s)` plus 10 %; `○` for each trail entry, `●` for `eta.at`, `│` for now; labels where a later mark displaces whole earlier labels. The future track and old ETAs are `inactive`; the now mark `│` is the default colour (red is only for late); the ETA dot and its label are `success` before it is reached and `error` after.
- `UnitView(elements, columns, bodyRows, …)` draws:
  - the heading (`Phase N of M · <stage>`, `Ticket <id> · <n> merged · <stage>` on a standing plan, or the stage alone), the title, the plan path when it exists, the summary;
  - ETA with overdue, gate and finish, then the ruler;
  - the trail as `HH:MM said HH:MM → …`, then the strip;
  - `This phase` steps, each label fitted to one row with two spaces before its time;
  - one row per worker, with slot, role and state padded to the widest entry;
  - the phase record, newest first.

  It keeps within `bodyRows`:
  - The phase record keeps the open row and the newest rows, with id, timing, fix-mark and ETA columns each one width.
  - Older done rows fold into `+N earlier: <ids>`, and todo rows past the next three into `+N later`.
  - Older steps fold into `+N earlier steps`.
  - An empty record reads `no phases recorded`. A block with nothing in it is not drawn.
- Accounts: `AccountsView(elements, columns, status, zone, now)` uses the pane's `bodyColumns`; every drawn line is at most `columns` cells, with no `…`. It draws the table shared with the cargo-handler accounts table (handler-enh builds that side to the same spec; send any change to handler-enh too). Columns, in order: a mark with no header, `account`, `role`, `left`, `resets`, `credits`, `enabled`, `in use`, `read`, under a bold lower case header row; every column is always shown. Each column is as wide as its widest cell or header (`cellWidth`), with 2 spaces between columns and no padding after the last. The mark `●` is `success` when `read_ok`, `error` when not or when the field is missing: it is the only access signal. `account` is bold; `role` joins roles with `, `; `left` is `60%` or `—`, `error` under 10 %, `warning` under 25 %, the only quota color; `resets` is a short time; `credits` is `13,446` or `unlimited`; `enabled` is `yes` when `use_credits`; `in use` is `yes` (in `warning`) when `credits_in_use`; `read` is the age of the reading (`45s`, `12m`, `2h`, `3d`, floored). Other missing cells are blank. `account`, `role`, `enabled` and `in use` are left-aligned; `left`, `resets`, `credits` and `read` are right-aligned, so `%` signs line up. Accounts sort by label (`localeCompare`, `sensitivity: 'base'`). The form is picked once for the whole list: the table when it fits; else a two-row table (row 1 the mark, `account`, `role`, `left`; row 2, indented 2, the other columns; a two-row header in the same shape); else a narrow form with no header (`● <label>  <roles>`, then the facts with their words joined by ` · `, a break only between facts). Times are short, in the resolved zone: same day `14:00`, within six days `Sat 14:00`, else `24 Oct`. Problems follow after a blank line: `Problems` in bold, then `• <text>` in `warning`, wrapped with indent 2. `accounts.py status --json` sends each problem without the text table's `* ` mark, and sends `read_ok` (a stored reading whose latest read outcome is `OK` or `HELD`) and `credits_in_use` (`on_credits == "yes"`). There is no Login column. `accountDisplay` maps the wire shape into `AccountReading<T>` and `CreditBalance`.
- `resolveUserZone(calls, isActive): Promise<ZoneLookup>` follows [`scripts/lib/user_zone.py`](../../scripts/lib/user_zone.py): `USER_ZONE`, then the `zone=` line of `USER_ZONE_CONF` or `~/.claude/config/user_zone.conf`, then `America/New_York`, validated through `Intl.DateTimeFormat`; it returns `{kind: 'cancelled'}` once the lifecycle moves on.

### The short report

`progress_history.py progress` (and `_print_last_recorded` between windows) prints by default the clocks table (`_summary_table`), the phase heading, one ETA line from `_phase_eta_line(session_dir, phase_number)`, the stage line and one `- **Agent N**` line per worker. `--tables` adds the round table with its `Earlier:` line; verification gate lines print in both forms.

- ETA line forms: `ETA HH:MM (first HH:MM, +H:MM, moved N×, …)`, `ETA HH:MM (on its first ETA, …)`, a leading `passed` when an open phase is overdue, fix rounds as `N fix rounds`, `+ 1 running` or `1 fix round running`, `gated until Wed 22 Oct 11:47`, `gated by <gate>: <Waits on>`, and `ETA unavailable` when no plan, state, row or promise exists. A promise on another local day shows weekday and date. The line reads only the reported phase's own row and gate.
- Worker lines come from `_current_worker_windows` / `_current_worker_cells`: per slot, the live window, else the most recent finished one, independent of the director's own activity.
- In the clocks table Project ETA is the chain's finish (`_project_chain_eta`), low/high shifted by the open phase's band; under an untimed gate it reads `after G<k>` with `—` for low/high.
- The legacy (non-dual) layout keeps its old form plus one ETA line after the phase line, and prints the clocks table only under `--tables`. The vault phase note keeps the full phase list.
- [`commands/unit/report.md`](../../commands/unit/report.md): `/unit:report [tables|on|off]` presents phase and stage, ETA line, the summary table copied byte for byte, worker table, summary sentences, then `See /showrunner:unit_board for the board.`; `tables` passes `--tables` and adds the round table after the summary table, with `Earlier:` and the verification gate lines. `<DelegationResultFormat>` in [`commands/unit/direct.md`](../../commands/unit/direct.md) closes every delegation result and launch with this short report.

## Invariants

- **Nothing shows by itself.** A pane opens only from its slash command or a press inside an open pane, and closes from its command, Escape or its close mark. No pane at `session.start`, no `AbovePrompt` band, no status line, no toast.
- **A closed pane does no work.** With a pane closed there is no timer, no `$.fs` call and no `$.process.run` for it; with no board pane open, `turn.complete` passes straight to `next`, and it never refreshes accounts. While open, refresh is `$.fs.stat` of existing files, and a process starts only on open or when a stat shows a change.
- **No new long-running process.** No daemon, service or timer job exists outside the mod; the generator and `accounts.py` are one-shot runs.
- **The mod never opens a credential file and never asks for a login.** It never reads `.claude.json`, `auth.json` or any `id_token`, and `accounts.py status --json` has no login field, so no login address reaches the mod.
- **A failed accounts run shows a fixed reason**, never process output, in the pane, a command reply, a log, a toast or a message.
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
- The dot and the percent are `error` under 10 % and `warning` under 25 %; at 25 % or more the dot is `success` and the percent the default colour; `no reading` has an `inactive` dot (`leftTone`). ` in use` is `warning`. `inactive` is used only for the words `resets`, `credits`, `read` and the separators; labels, roles, numbers and times are the default colour. No account text is `subtle`, which made the table hard to read.
- `HH:MM` for `--time`, `--earliest`, `--latest` means today only; a later-day ETA needs the full date.
- A phase record's `first_eta` comes from `_instance_eta_events`, so a checkpoint-notice ETA recorded before the phase starts counts as its first.
- `build_plan` can select an older resumed phase as open, so the newest `phase_started` is the wrong instance for `steps` and `stage`; both follow `OpenPhase.instance_id`.
- `phase_table show --json` drops gates and turns `GateBlockedFinish` into null; the board builds from `build_plan`'s record instead.
- Taking worker cells from the round table's last row blanks them once the director opens an activity; reading the next gated phase instead of the reported one gives the wrong ETA line.
- A failed stat counts as a change: a `failed` board pane retries each tick while a source stays newer than the board file, and recovers on the next good run.
- A close during a pending `session.start` load or zone lookup must not restart the accounts timer; the lifecycle counter guards it.
- The mod runs the generator and `accounts.py` installed under `~/.claude/scripts`, not a worktree's copy, so a live check of the mod proves only promoted code; a contract change can only be checked live after it reaches main.
- `accounts.py status` with no flags can print login addresses in its problem notes. Never run it to look up an account label; take the label from the folder name (`~/.claude2` → `claude2`).
- A fresh checkout's `tsc` reports `TS2307` for `claude-code` until a live session loads the folder and the engine writes `mods/unit-board/.claude-plugin/types/`.
- Tests patch `plan_history.now_epoch` for the clock.
- A session loads mod hooks when it starts. A session started before a mod change keeps the old hooks until `/reload-plugins`, and the command file's fallback line answers in their place; the line names `/reload-plugins` for that reason. The natedev showrunner hit this on 2026-10-18.
- The board reads the style ledger with `sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0)`, never through `style_ledger.Ledger`, which takes a write lock (`BEGIN IMMEDIATE`) and loads the rule catalog. SQLite's default 5 s busy timeout made a locked ledger stall each standing unit in turn; `timeout=0` gives 0 at once. `style_plan.py` writes a ticket heading as `### Phase <n> — <ticket id>`, so the phase id is the bare number; the ledger's open ticket names the ticket, and it can run one ticket ahead of the run record after refill.
- The Units report (`style_units.py`) counts recent built findings with `built_at` on schema 25. A ledger before schema 25 has no such column; the report then uses `style_ledger.legacy_built_at_sql("h")`, the rule migration 25 uses to fill `built_at`: the merged build ticket's `updated_at`, else the finding's `updated_at`. A ledger without the `tickets` table or the `build_ticket` column uses the finding's `updated_at`, because that SQL fails there. The report stays read-only and never migrates the ledger. The totals ETA uses the same recent count.

## Why

The recorder already held everything the board needs, but as tables written for a terminal, and a unit's ETA lived only in the director's prose. Recording each stated ETA, with the phase it names and its basis, turns promises into data: the board can show the first promise, every move and the latest one, and the chain can predict later phases from what the director actually said rather than from a projection. Recording before stating, and stating the recorder's printed time, keeps the spoken ETA and the recorded one identical.

The ETA shown is the latest promise because a passed promise advanced to now would read as on time while the phase runs late. The chain still advances it for scheduling, since later phases cannot start in the past; the two values are kept apart (`eta.at` versus `schedule_end`) so neither leaks into the other.

The mod draws and Python computes. The chain, gates and history live in `phase_table`, which is stdlib Python under `unittest` and basedpyright; a generator that writes one JSON file keeps a single source of predictions, a typed contract tested on both sides, and a view layer with no logic to drift. `state` comes from the unit's own newest run, and `local_run` reads each file only to its `run_started` line so the board opens inside the process limit. Other production watchers share one `unit_lookup.read_run_states()` snapshot per tick, reading each file's first line and each worktree's newest record's last JSON object; `run_state` is the wrapper for a single lookup. `build_run` keeps a unit's record on the board after its plan turns into an as-built doc and loses its phase headings.

The panes open only on demand because the user asked for it, and a closed pane costs nothing for the same reason a background watcher was ruled out: no session should pay for a view nobody is looking at. While open, stat-based refresh is cheap: the sources are files the run already writes, so an unchanged tick is a handful of stats and a redraw, and the generator runs only after something changed. The `discovery` paths exist so a production or input that appears later still triggers a refresh without a scan.

The accounts view draws `accounts.py`'s stored status straight to the screen. It first showed login addresses; the user removed the Login column (2026-10-19) to give the table room, so the mod no longer asks for logins at all. It reads stored values (`--stored`) rather than probing accounts, and reruns only when one of its four input files changes, so an open pane spends no quota and no network.

The accounts view became account lines (phase 18) because the user could not read it: the fixed table was about 88 columns wide and ignored the pane width, so in a docked pane each row wrapped through its columns. The lines follow the phase 17 board style. One shape for every account, aligned columns and whole facts make the list easy to scan at any width.

The user then asked for column headers and alignment in columns (phase 19): the account lines put each fact at a different column, because short times and credit parts have different widths. The table came back with a header row, right-aligned times and numbers, and two-row and narrow forms so it still fits a docked pane. The JSON problems lost the text table's `* ` mark, which the pane drew after its own bullet.

The user then asked that `/accounts-board` and the cargo-handler accounts table show the same thing (phase 20). The column set was settled with cargo-liner's showrunner: the green/red mark the user liked became an access mark from `read_ok`, `left` replaced `weekly left`, and `enabled` and `in use` became columns. Both views read the same `accounts.py` JSON, so they cannot disagree.

The terminal report leaves out the round table because it answers questions most ticks do not ask; the board carries the visual view, and `tables` keeps the full recorder output one word away.

A standing plan shows its ticket and a merged count, not `Phase N/total`, because it holds one ticket phase at a time and refill discards each merged one: `Phase 63/1` read as phase 63 of 1 when it was the 63rd ticket. The merged count, not a total, is the progress a ticket queue has.

The board lists units by name and keeps each one to three short lines because the user found it hard to read (2026-10-19): units were sorted by state, most text was dim grey, and titles and worker lists were cut off. Three design reviewers agreed on the rest: colour carries meaning (the dot is health, red is only for late), every fact is in readable text, worker names move to the detail view in favour of counts by state, and the ruler, whose labels repeated the same now time on every unit, moves to the detail view. `Phase <id>` drops `/<of>` because a plan that grew read `Phase 32/31`; the total is beside the bar instead.

The board finds its production from the session id before the working directory, because a showrunner session need not run inside its production's checkout: the natedev showrunner runs in `/etc/nixos` with its doc under `~/.claude`. The update timer is addressed to the session id, so `showrunners.production_of` answers exactly.
