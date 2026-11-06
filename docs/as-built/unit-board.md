# Unit board

## What it is

The mod in `mods/unit-board/` draws three panes on demand.
`/showrunner:unit_board` shows the production, its child productions, or one unit.
It shows phases, ETAs, gates, finish times, and workers.
`/showrunner:accounts` shows stored account readings without login addresses.
`/build:queue` shows the memory cap, room, running steps, and waiting steps.
The recorder keeps each phase's stated ETA and predicts later phases from it.
`/unit:report` prints a short report; `tables` adds the recorder's round table.

## How it works

### Data flow

```
director states an ETA ──► progress_history.py eta ──► eta_stated event (runs/<run_id>.jsonl)
                                                              │
                         phase_table.build_plan(plan) ◄───────┘  (chain, gates, PhaseHistory)
                                  │                        │
            progress_history.py progress           board_json.py --session <id>
            (short report / --tables)              writes <staff-id>.json, prints {files, watch}
                                                           │
                                       mod: /showrunner:unit_board ── reads JSON, stats sources while open
                                       mod: /showrunner:accounts ── runs accounts.py status --json --stored
                                       mod: /build:queue ── runs build_queue.py --json
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
~/.claude/scripts/lib/py ~/.claude/scripts/direct/board_json.py (--session <id> | --production <showrunner name>)
```

[`board_json.py`](../../scripts/direct/board_json.py) reads the staff file to resolve each production. It writes each board atomically to `${UNIT_BOARD_DIR:-${XDG_STATE_HOME:-~/.local/state}/unit-board}/<showrunner-staff-id>.json`. It prints one line: `{"files": [...], "watch": [...]}`. The plugin reads these returned paths directly. A rename, transfer, or session replacement preserves the cache path. An unresolved production returns `"files": []` and exits 0. An unreadable staff file also prints its cause to stderr.

- `board_json.resolve` reads one staff snapshot for all boards. It finds the session with `record.by_current_session`, then follows staff parents to the first showrunner. Unit and worker sessions see that production and its descendant productions, nearest level first. `--production <showrunner name>` selects the same subtree by staff name and takes precedence over `--session`. The resolver reads document paths from showrunner staff rows and zones through `showrunners.timer_for`. It excludes showrunners without timer registrations. A session without a staff row or a showrunner ancestor produces no files.
- `watch` holds the notifier registry dir (`NOTIFIER_STATE_DIR`, default `~/.local/state/notifier`), so an open pane notices a production started later. It also holds `SHOWRUNNER_STATE_DIR` and `SHOWRUNNER_STATE_DIR/<staff id>` for every registered showrunner: `showrunners.attach` and `detach` write or remove `<child>/parent`, which moves that dir's mtime. Existing paths only; registration creates `<showrunner name>/` before the update timer, so the dir exists before any attach.
- The JSON contract is [`mods/unit-board/types/board.d.ts`](../../mods/unit-board/types/board.d.ts), field for field, and [`test_board_json.py`](../../scripts/direct/test_board_json.py) checks every output key against it. Top level: `production`, `as_of`, `zone`, `utc_offset_minutes`, `units`, `sources`. Every time is `BoardTime {at: epoch s, text}` in the production's zone, `text` `HH:MM` today, else `ddd DD Mon HH:MM`.
- `BoardUnit`: `key` (the staff name), `plan`, `state` (`running` | `run-finished` | `standing-by`, from the unit's own newest run via `unit_run.local_run`), `phase` (tagged by `kind`: `plan {id, of, title}`, or `ticket {id, title, merged}` when the plan holds a `> **Standing:**` line; one read-only ledger read gives `merged`, the unit's `merged` tickets, and `id`, its newest `assigned` or `checkpointed` ticket (the current phase is then the open `phase_table.build_run` row whose phase or leading title word is that id, so a ticket that a unit rename moved still shows under its old prefix), else the title's leading `<unit>-<n>`, else the phase id; `title` drops that leading id) or null, `phases` (`BoardPhase`: `status` `done|open|todo|gated`, `took`, `fix_done`, `fix_running`, `findings {blocker, minor, nit}`, `eta {first, trail, finished, delta_minutes}`, todo `start`/`finish`, `gate` `none|timed|untimed`; a non-`none` gate makes the status `gated`), `finish` (`predicted`/`finished` with `at`, `after_gate` with `gate`/`waits_on`, or `unknown`), `stage`, `eta` (`BoardEta {at, first, trail, moved, delta_minutes, schedule_end, fix_done, fix_running}`) or null, `summary` (newest `progress_reported` text), `steps` (`[state, what, started, took_seconds]` for the open instance's passes and activities), `workers` (`slot`, `role`, `state`, `model`, `doing`).
- `stage`, in order:
  - the open pass as `Build`/`Tests`/`Review`/`Repair round <n>`;
  - An unlanded `finding_batch_dispatched` in the open phase appears as `Repair round <dispatched batches>`. This label shows dispatch records for audit. Repair counts require started fix passes. Relaunches of one ledger batch add no round. Refunded batches add no round unless they later land.
  - the open activity label (`display_cells.open_step`);
  - `Phase <id> running` while a phase is open (`Ticket <id> running` on a standing plan);
  - otherwise `Run done`, `Waiting on G<k>` or `Standing by`.

  `Standing by` therefore means no phase is open.
- `state` comes from `unit_run.local_run`, which reads each run file only up to its `run_started` line (every run file holds one, on line 1) and skips lines without `"run_started"` before parsing.
- The record is built from the found run's own plan (`project_plan_doc`, else `plan_doc` resolved against the worktree), falling back to the Plan cell's plan.
  - It is `build_plan` on that plan; when that has no phases, `build_run(plan, events)` from the run's events.
  - A run on another plan than its production row (hana `demo`) shows its real phase.
  - `plan` is the Plan cell, and is empty when that file no longer exists.
- `workers` combines the run roster, slot metadata, and each slot's newest board activity.
  `recorded_turn_settings` supplies the accepted model and effort.
  `done` displays as `idle`; `ended` stays unchanged.
  The reader displays raw role and family values when the records contain them.
  Missing values display as empty fields.
  The [worker design](long-lived-workers.md#roster-fields) defines the current records without copied role or family fields.
  - Slots drop the worktree's mesh prefix.
  - Roster entries that become one slot merge into the entry its `impl_worker_<slot>.json` names, else the last in roster order. Roster `revision` values are per-entry counters, never compared.
  - `doing` is less a leading `launcher: ` and a trailing `; summary at …` or `; findings in …`, stripped and cut to 80 characters.
- `eta.at`/`first`/`trail` are stated promises only; `schedule_end` is the chain's end and never a trail entry.
- `sources` lists existing paths only: each unit's run `.jsonl`, `mesh_roster.json`, `board.log`, every `impl_worker_<slot>.json`, the plan doc, every `plan_runs` `.jsonl`, a standing unit's style ledger, the production doc, plus each session dir, the run roots and the history `runs/` dir (or the history root while `runs/` is missing), so a run that starts later moves a watched mtime.

### The mod

`mods/unit-board/` is a Claude Code mod (TypeScript, JSX with `h`, no DOM, no Node; every engine call goes through `$`). [`hooks/hooks.json`](../../mods/unit-board/hooks/hooks.json) loads [`register.tsx`](../../mods/unit-board/hooks/register.tsx); [`mods/.claude-plugin/marketplace.json`](../../mods/.claude-plugin/marketplace.json) (`nate-local`) lists it beside `postmaster`. Without an install, a session loads it only with `--plugin-dir ~/.claude/mods/unit-board`. To make it available in every session on a machine, run `claude plugin marketplace add ~/.claude/mods` and then `claude plugin install unit-board@nate-local`. Together these add `nate-local` to `extraKnownMarketplaces` and `unit-board@nate-local` to `enabledPlugins` in `~/.claude/settings.json`. Claude Code 2.1.316 and later reads the mod in place from `~/.claude/mods/unit-board`, because `nate-local` is a local folder marketplace and lists the mod by a relative path. `claude plugin list` shows `Read from: ~/.claude/mods/unit-board`. A promoted change takes effect at the next session start or `/reload-plugins`, and no copy exists in `~/.claude/plugins/cache`. A session started on an older Claude Code loads a copy from that cache instead. Restart such a session on the current Claude Code.

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
| `hooks/queue.tsx` | `QueueView`: paint the Python reader’s span lines in order, with foreground/background tones and boldness; draw recorded countdowns from the current clock and clip at the pane edge. Layout stays in the reader. |
| `types/board.d.ts`, `types/accounts.d.ts`, `types/queue.d.ts` | Wire contracts of the board, accounts and build queue readers. |
| `types/index.d.ts` | `BoardSnapshot`, `PaneState`, `AccountsPaneState`, the `PluginState` keys. |
| `tests/` | `kit.ts` fixtures; `geometry`, `register`, `accounts` and `queue` tests. |

**Commands.**

- `/showrunner:unit_board [<unit>|close]` opens pane `unit-board` (title `Units`, closes on Escape), stores `loading`, runs the generator once with `--session <$.session.id()>` (the open pane keeps the session id for each refresh), reads each listed file and stores the unit view for `<unit>` (the first board holding that key), else the board view. With no files the pane shows `This session is not on the staff list.`; it replies `Unit board opened.` Pressing a unit name on the board and the `board` button on a unit switch views. `close` replies `Unit board closed.`
- A mod command name allows only letters, digits, `_` and `-`, so [`commands/showrunner/unit_board.md`](../../commands/showrunner/unit_board.md) gives the name. The mod answers it with a `command.run` hook on `{ command: 'showrunner:unit_board' }` that returns `{ text }` and never calls `next`, so no model turn starts. In a session without the mod, the file's body makes the model reply one line naming `unit-board@nate-local`.
- `/showrunner:accounts [close]` opens pane `accounts`, titled `Accounts`, without a model turn.
  It resolves the user zone and stores `loading`.
  Each open and refresh runs `[~/.claude/scripts/lib/py, ~/.claude/scripts/accounts/accounts.py, "status", "--json", "--stored"]`.
  It replies `Accounts opened.`; `close` replies `Accounts closed.`.
- `/build:queue [<position|session|folder>]` is intercepted by `command.run` without a model turn. It opens `build-queue` (title `Build queue`, Escape closes), stores `loading`, runs `[~/.claude/scripts/lib/py, ~/.claude/scripts/build_queue/build_queue.py, "--json", "--columns", <last bodyColumns>, "--you", <session id>, <optional selector>]` and replies `Build queue opened.`. The width starts at 100. A pane-width change requests one forced refresh; the shared busy guard joins repeated requests. A selector shows the full queue, one blank line, and the matching cards on one page. `mine` uses the same layout. The selection survives the 10 s refresh and reload. `close` replies `Build queue closed.`. Bump retains its existing refusal handling and refreshes an open pane. Bump is off; nothing can move a waiting step.
- `queue_lines.py` defines `Span {text, tone, bold, background, until?}`, terminal-cell widths, padding, clipping, hanging-indent wrap, text output and `QueueLines {columns, as_of, lines}` JSON. Tone names include blue `suggestion`. JSON omits fields at their `Span` defaults: `tone` and `background` `default`, `bold` false, and no `until`. The pane accepts missing fields as those defaults; older explicit fields remain valid. An `until` span contains only the countdown text and its epoch deadline; the pane computes `max(0, until − now)` on every draw. One `$.clock.every(1000, …)` timer invalidates the drawing while a countdown is active, without a script run; it stops on expiry, a page without a countdown, or close. The 10 s script refresh stays.
- `build_queue.py` reads one captured admission pass through the bounded `queue_read()` query.
  `unit_labels_for` supplies the labels through one file read.
  The memory bar shows the cap, anonymous use, keep-aside, room, safety, and other programs.
  Running rows show each run and its held slot.
  Waiting rows show the showrunner, unit, step, waiting minutes, pid, and need.
  Every process row shows its captured `pid N`, or `pid —` when no pid was recorded.
  The pid column takes space from the row bar while names keep their allocated widths.
  The first eligible row has an `↑` marker and a recorded hold cause.
  Head protection keeps its full need available before a younger run starts.
  Held slots give the same run priority for its next step.
  New starts use the recorded start-spacing deadline.
  Service rows retain RAM and swap amounts through marked continuation lines.
  `--you` selects the session through `$.session.id()`.
  A blue `▸` marks matching rows.
- Selector cards share the queue model and the admission check formatter.
  Each card shows the captured room check, start basis, head cause, and run keep-aside.
  The evidence uses the same pass as the queue view.
  A failed refresh retains the last successful view.
  A failed first query shows only `Build queue could not be read.`.
  Times use the user’s zone; `as_of` is the admission reading’s epoch.
- `session.start` restores refresh timers for open panes. Command files supply the colon names; mod hooks open the panes without a model turn. Without the mod, `/showrunner:accounts` names the required plugin. `/build:queue` runs its script. Run `/reload-plugins` in sessions that need the updated hooks.

**State.** Plugin state `unit-board` holds the board and accounts records, each keyed by pane id; a missing entry is a closed pane.

- `panes`: `PaneState<BoardData>` = `loading` | `board {data, now}` | `unit {data, production, unit, now}` | `failed {reason}`. `data` is a `BoardSnapshot {files, watch, loadedAt, boards}`.
- `accountsPanes` uses `AccountsPaneState<StoredAccountsStatus>`: `loading`, `accounts {data, zone, now}`, or `failed {reason}`.
  Each load sets `now` in seconds.
  A tick updates local liveness when the generator reports session inputs.

- `queuePanes`: `QueuePaneState` = `loading {selection}` | `queue {data: QueueLines, selection}` | `failed {reason, selection}`. `QueueSelection` is `{kind: 'view'} | {kind: 'card', selector: string}`. The one load boundary converts older saved string selectors to cards, and null or missing selectors to views; refresh and reload retain the named selection. `QueueOpening` reuses that selection type beside its `saved` case. Width stays in module-level `queueColumns` and `QueueWidthRefresh`. It belongs to the same `unit-board` state namespace, under its own pane id.

Timers stay module-level and are never persisted. The board retains `OpenPane {timer, session, busy, paths, selection}` (`Selection = {kind:'board'} | {kind:'unit'; production; unit}`). Accounts and queue use the same `tablePane` helper in `register.tsx`: open/loading, one reader at a time, a 10 s timer, fixed failure reason, reload and close. Each instance keeps its own context, watched paths and lifecycle counter. Close invalidates pending work and zone lookups; a forced refresh during a read is coalesced into one following read. Paths and board selection survive a failed refresh.

**Refresh on change.** The board and accounts panes share:

```ts
refreshIfChanged<T>(files: RefreshFiles, paths: readonly RefreshPaths[],
  generator: () => Promise<T>, set: (fresh: T) => Promise<void>, isOpen: () => boolean): Promise<boolean>

type RefreshPaths =
  | { kind: 'board'; board: string; sources: string[] }       // sources vs the board file's mtime
  | { kind: 'discovery'; sources: string[]; loadedAt: number } // sources vs the load time
  | { kind: 'liveness' } // refresh local counts each tick
```

A source newer than its reference, or a failed stat, triggers one generator run.
`isOpen` guards each stat, generator call, and state write.
The production board refreshes every ten seconds and on `turn.complete`.
An unchanged production board tick advances its display clock without a process.
The accounts pane uses a separate ten-second timer and a busy guard.
It ignores `turn.complete`.
`loadAccounts` watches saved usage, groups, worker selection, and the role-map cache.
It also watches reported `session_inputs`, including the peer-count cache.
A missing file uses its nearest existing parent as a discovery source.
A `liveness` refresh policy requests current local counts on each open-pane tick.
`ui.close` cancels that pane's timer and removes its state.
The build queue reads process memory every ten seconds while open.

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
- Accounts: `AccountsView(elements, columns, status, zone, now)` uses the pane's width.
  The columns are the access mark, `account`, `left`, `role`, `busy`, `shell`, `idle`, `total`, `resets`, `credits`, and `enabled`.
  The table omits the `read` and `in use` columns.
  Account labels are bold, and numbers align to the right.
  Each role has a separate row with its own counts.
  The first row shows all account values.
  Later rows show only the role and its counts.
  Python combines available machine counts once in `role_counts`, in role order.
  The view reads these counts directly; `role_counts_by_machine` keeps the separate machine records.
  A role with no reported members has zero counts; `spare` has no count.
  A totals row sums the known activity counts.
  Codex shell counts remain unknown; they do not become zero.
  The access mark uses `read_ok`: success for a recent usable reading, and error otherwise.
  Low allowance uses error below 10 percent and warning below 25 percent.
  Tables use the widest cell or header, with two spaces between columns.
  A wide table uses one row per role.
  Narrower panes split the columns or use labeled facts.
  The labeled facts repeat no account values for later roles.
  The narrow form breaks between complete facts and keeps each drawn line within the pane.
  Times use the resolved user zone.
  Problems follow the table as wrapped warning bullets.
  `accountDisplay` maps the wire contract into `AccountReading<T>` and `CreditBalance`.
  The [account design](accounts.md#status-counts-and-notes) defines count attribution, peer availability, and stored reads.

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
- **A closed pane does no work.** With a pane closed there is no timer, no `$.fs` call and no `$.process.run` for it; with no board pane open, `turn.complete` passes straight to `next`, and it never refreshes accounts. While open, board and accounts refresh through `$.fs.stat` of existing files; the queue runs its reader every 10 s for live measurements.
- **No new long-running process.** No daemon, service or timer job exists outside the mod; the generator, `accounts.py` and `build_queue.py` are one-shot runs.
- **The mod never opens a credential file and never asks for a login.** It never reads `.claude.json`, `auth.json` or any `id_token`, and `accounts.py status --json` has no login field, so no login address reaches the mod.
- **A failed queue refresh keeps the last successful page.** A failed first query shows a fixed reason. Accounts failures show a fixed reason. No failure displays process output.
- **User-facing times use the user zone.** Python writes them with `astimezone(user_zone())`; the mod resolves the zone in `scripts/lib/user_zone.py`'s order; board times use the production's registered zone. `scripts/lib/test_user_zone` scans every script.
- **The ETA is the latest stated promise**, never the schedule end an overdue phase is advanced to. `eta.at`, `first` and `trail` hold promises only; `schedule_end` is never a trail entry; the ruler's ETA mark is `eta.at`.
- **Readers place an ETA by `eta_phase_id`**, never `phase_id`, which names the phase active when it was recorded.
- **The mod never computes ETAs or predictions**; it draws the board JSON. The recorder never writes the board file; only `board_json.py` does.
- **`board.d.ts` is the generator's contract**, and every output key is tested against it.
- **Imports stay one-way.** `phase_table` imports `plan_history`, never `progress_history`; `progress_history` imports `phase_table` only inside function bodies (`_project_chain_eta`, `_phase_eta_line`). basedpyright's import-cycle check is file-level with no line anchor, so a cycle must be removed, not suppressed.
- **The board, accounts and queue panes are independent**: separate state records, timers and lifecycles, so each opens and closes without touching the others. Engine calls stay in `register.tsx`.
- `settings.json` is never edited to install the mod. `.gitignore` allows `mods/**` and excludes `mods/**/.claude-plugin/types/`.

## Calibration and gotchas

- Refresh interval: 10 s for all panes (`$.clock.every(10_000)`), plus `turn.complete` for the board.
- Phase strip: above 30 phases, or more phases than the room, it shows `N/M done` instead of cells.
- Ruler horizon: 10 % beyond the later of the promise and now; board ruler width is `bodyColumns - 4`, unit `bodyColumns - 2`, minimum 8.
- `doing` is cut to 80 characters, after the `launcher: ` prefix and the summary or findings path are removed.
- `local_run` stops at the `run_started` line: parsing every line of every run file once per unit took 35 s for three productions, past the mod's 30 s process limit. Reading to that line brings it to about 4.5 s.
- The access dot uses `read_ok`; it does not use the quota colour.
  The percent uses error below 10 percent and warning below 25 percent.
  Other quota values use the default colour.
  Missing quota shows a dash.
  The table has no `read` or `in use` column.
- `HH:MM` for `--time`, `--earliest`, `--latest` means today only; a later-day ETA needs the full date.
- A phase record's `first_eta` comes from `_instance_eta_events`, so a checkpoint-notice ETA recorded before the phase starts counts as its first.
- `build_plan` can select an older resumed phase as open, so the newest `phase_started` is the wrong instance for `steps` and `stage`; both follow `OpenPhase.instance_id`.
- `phase_table show --json` drops gates and turns `GateBlockedFinish` into null; the board builds from `build_plan`'s record instead.
- Taking worker cells from the round table's last row blanks them once the director opens an activity; reading the next gated phase instead of the reported one gives the wrong ETA line.
- A failed stat counts as a change: a `failed` board pane retries each tick while a source stays newer than the board file, and recovers on the next good run.
- A close during a pending `session.start` load or zone lookup must not restart the accounts timer; the lifecycle counter guards it.
- The mod runs the generator, `accounts.py` and `build_queue.py` installed under `~/.claude/scripts`, not a worktree's copy, so a live check of the mod proves only promoted code; a contract change can only be checked live after it reaches main.
- The accounts view uses numbered labels and requests no login column.
  Stored reads do not wait for the detached counts fetch.
- A fresh checkout's `tsc` reports `TS2688` for `claude-code`, `claude-code-tools` and `claude-code-mcp` until a live session loads the folder and the engine writes `mods/unit-board/.claude-plugin/types/`.
- Tests patch `plan_history.now_epoch` for the clock.
- A session loads mod hooks when it starts. A session started before a mod change keeps the old hooks until `/reload-plugins`, and the command file's fallback line answers in their place; the line names `/reload-plugins` for that reason. The natedev showrunner hit this on 2026-10-18.
- The board reads the style ledger with `sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0)`, never through `style_ledger.Ledger`, which takes a write lock (`BEGIN IMMEDIATE`) and loads the rule catalog. SQLite's default 5 s busy timeout made a locked ledger stall each standing unit in turn; `timeout=0` gives 0 at once. `style_plan.py` writes a ticket heading as `### Phase <n> — <ticket id>`, so the phase id is the bare number; the ledger's open ticket names the ticket, and it can run one ticket ahead of the run record after refill.
- The Units report (`style_units.py`) counts recent built findings with `built_at` on schema 25. A ledger before schema 25 has no such column; the report then uses `style_ledger.legacy_built_at_sql("h")`, the rule migration 25 uses to fill `built_at`: the merged build ticket's `updated_at`, else the finding's `updated_at`. A ledger without the `tickets` table or the `build_ticket` column uses the finding's `updated_at`, because that SQL fails there. The report stays read-only and never migrates the ledger. The totals ETA uses the same recent count.

## Checks

From the repository root, run `command claude plugin test mods/unit-board` for the whole mod suite and `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/unit-board` for the mod type check. The ignored `.claude-plugin/types/` declarations from a loaded session are required by `tsc`; the test command alone did not generate them in this checkout.

The queue reader checks cover `test_build_queue`, `test_head_facts`, `test_queue_view` and `test_queue_card`; their shared file fixture produces golden renders at 80, 100 and 140 columns and the first and fourth cards below the full queue view. A live pane check requires the installed code and `/reload-plugins`: compare its rows and head explanation with the script at the same time.

## Why

The recorder already held everything the board needs, but as tables written for a terminal, and a unit's ETA lived only in the director's prose. Recording each stated ETA, with the phase it names and its basis, turns promises into data: the board can show the first promise, every move and the latest one, and the chain can predict later phases from what the director actually said rather than from a projection. Recording before stating, and stating the recorder's printed time, keeps the spoken ETA and the recorded one identical.

The ETA shown is the latest promise because a passed promise advanced to now would read as on time while the phase runs late. The chain still advances it for scheduling, since later phases cannot start in the past; the two values are kept apart (`eta.at` versus `schedule_end`) so neither leaks into the other.

The mod draws and Python computes. The chain, gates and history live in `phase_table`, which is stdlib Python under `unittest` and basedpyright; a generator that writes one JSON file keeps a single source of predictions, a typed contract tested on both sides, and a view layer with no logic to drift. `state` comes from the unit's own newest run, and `local_run` reads each file only to its `run_started` line so the board opens inside the process limit. Other production watchers share one `unit_lookup.read_run_states()` snapshot per tick, reading each file's first line and each worktree's newest record's last JSON object; `run_state` is the wrapper for a single lookup. `build_run` keeps a unit's record on the board after its plan turns into an as-built doc and loses its phase headings.

Panes open on demand, so a closed pane runs no view refresh.
The production board uses file changes to trigger its generator.
Discovery paths detect a new production or input.
The accounts pane also refreshes local liveness each tick when its generator reports session inputs.
The build queue refreshes process memory each tick.

The accounts view uses `accounts.py status --json --stored` to read saved usage without account probes.
It watches usage, group selection, worker selection, role-map cache, peer-count cache, and reported session inputs.
When the generator reports session inputs, each open-pane tick refreshes local liveness counts.
The stored read uses an existing peer-count cache without waiting for its refresh.
Python combines available observations in `role_counts`; the separate machine records and problem lines identify a missing peer.
Aligned headers and complete facts keep the same contract readable in wide and narrow panes.


The terminal report leaves out the round table because it answers questions most ticks do not ask; the board carries the visual view, and `tables` keeps the full recorder output one word away.

A standing plan shows its ticket and a merged count, not `Phase N/total`, because it holds one ticket phase at a time and refill discards each merged one: `Phase 63/1` read as phase 63 of 1 when it was the 63rd ticket. The merged count, not a total, is the progress a ticket queue has.

The board lists units by name and keeps each one to three short lines because the user found it hard to read (2026-10-19): units were sorted by state, most text was dim grey, and titles and worker lists were cut off. Three design reviewers agreed on the rest: colour carries meaning (the dot is health, red is only for late), every fact is in readable text, worker names move to the detail view in favour of counts by state, and the ruler, whose labels repeated the same now time on every unit, moves to the detail view. `Phase <id>` drops `/<of>` because a plan that grew read `Phase 32/31`; the total is beside the bar instead.

The board uses staff for placement so a unit or worker sees its direct showrunner’s board from any folder. The newest pane launch supplies only the staff id; the staff row and its parent edges decide the board’s root and subtree.
