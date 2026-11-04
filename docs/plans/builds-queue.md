# builds-queue

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The user approved the queue service design on 2026-10-30 (`93c86342`); Phases 4, 5 and 10 come from it. `/build:queue` answers in under a second with one line per step; then one queue service process holds the queue, each build asks it over a socket and waits, and the polling admission goes.

> **Renumbered 2026-10-30:** Phase 2 (the approved queue view) was put in after Phase 1b; the old Phases 1c, 2, 3 and 4 are now Phases 3, 4, 5 and 6.

> **As-built disposition: amend** — `docs/as-built/unit-board.md` (the queue pane); `docs/as-built/build-memory-admission.md` (snapshot, `queue_read`, `LineWaiter` checks, `match_waiters`; later the service); `docs/as-built/build-targets.md` (the `/build:queue` row)

> **Production: builds** — unit `queue-unit`; production doc `docs/plans/builds-production.md`

## Source

2026-10-30 11:26 EDT

add a queue unit - it should own all existing queue scripts and skills - transfer any current work on report unit to queue unit that is queue related - queue can keep it running - and it should task an agent to start the design and the migration plan

2026-10-30 (the user, to the showrunner; relayed verbatim 11:4x EDT)

why does every build run the queue code - why don't we have a system where there is a queue that guards and releases?

should we use rust or python?

2026-10-30 ~11:25 EDT (the showrunner, relaying the user; its answer to rust or python: Python, for one memory model and no tool build)

The design: the user asked why every build runs the queue code, and wants a queue service that guards and releases. One Python process holds the queue. Each build asks it over a socket and waits. It admits in queue order, frees memory when a build ends, and serves override, priority and the view as commands. It needs a rule for when the service is down. Python, not Rust: one memory model, no tool build. Have an agent write the design and the migration plan from today's polling admission. The user reviews the design before any code.

2026-10-30 (the user, to report-unit, carried over with its Phase 4g)

build queue is unusable for me ... i can't parse this ... it is acceptable to be wider ... be more creative ... it is TOO slow

i need instantaneous answers on builds ALWAYS ... i cannot get clarity

i need to get sub second responses to any questions like these

only I can bump

## Delegation Context

- **Project:** `~/.claude` — the build queue: memory admission, the `/build:queue` view and evidence cards, and the unit-board queue pane.
- **Project started:** 2026-10-30T11:26:00-04:00
- **Stack:** Python 3.13 (basedpyright strict, unittest); TypeScript mod under `mods/unit-board/` (its own tests in `mods/unit-board/tests/`).
- **Layout:** `scripts/build_queue/` (`build_queue.py`, `queue_evidence.py`, `head_facts.py` from report-unit 4e); `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py`; `scripts/message/roster.py`; `mods/unit-board/hooks/` and `mods/unit-board/tests/`; `commands/build/queue.md`.
- **Key files:** `scripts/lint/memory_admit.py` — admission decisions, the queue rows and `explain_queue`; `scripts/build_queue/build_queue.py` — the queue view and the bump CLI; `scripts/build_queue/queue_evidence.py` — recorded need, charge, and release text; `scripts/build_queue/head_facts.py` — bump rows, head time, token waits; `mods/unit-board/hooks/register.tsx` — `tablePane()` and the `build:queue` command; `mods/unit-board/hooks/queue.tsx` — the pane view; `scripts/build_queue/admission_protocol.py`, `scripts/build_queue/admission_service.py`, `scripts/build_queue/admission_client.py` — the queue service, its socket protocol and its client (new in Phase 5).
- **Test lanes:** `scripts/build_queue/test_*.py`, `scripts/lint/test_memory_admit.py`, `mods/unit-board/tests/queue.test.ts`.
- **Test:** `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY python3 -m unittest <module>` from the worktree root; the mod's test command as `docs/as-built/unit-board.md` names it.
- **Lint:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python <the slot's own Python files>`; footer must read `0 errors, 0 warnings, 0 notes`.
- **Ownership (showrunner, 2026-10-30):** queue-unit owns `scripts/build_queue/` and `commands/build/queue.md` (from report-unit), and `scripts/lint/memory_admit.py` with `scripts/lint/test_memory_admit.py` (from resources-unit), and `scripts/lint/memory_gate.sh` (from resources-unit, with Phase 7; builds showrunner, 2026-10-31). queue-unit also owns resources Phases 15 and 27, now Phases 8 and 9 of this plan: one owner for admission changes in `memory_admit.py` (builds showrunner, 2026-10-31). resources-unit keeps its other in-flight `memory_admit.py` phases until they land (admit-head, queue order, `/showrunner:bump` priority, strict-window cap, 24B); until then each `memory_admit.py` edit is coordinated with resources-unit through berth, and resources-unit reviews the diff. `scripts/message/roster.py` is shutdown-unit's (natedev): moving `unit_labels_for` and its file-only helpers to the new `scripts/message/unit_labels.py` is approved (showrunner, 2026-10-30), and the notice carries an also-touches line. `mods/unit-board/` is natedev unit-board-unit's; the build queue parts of `register.tsx` change under an also-touches line.
- **Resources Phase 26 is built (58391add, on `origin/builds` at e2773be6, in this branch at d309bbed):** the queue order comes from `priority_rank` and the user's bump sets a priority in `memory_admit.py`; `bump_order`, `holds_younger` and `bump_waiter` are gone, and `priority_since` stays. Phase 4 carries its fields (Phase 4d). Phase 10 carries Phase 26's state into the queue service (Phase 10's Decided block, 2026-10-31).
- **Cut over, no shadow (user, 2026-10-31):** "we should cutover and not try to run the old and the new at the same time." Phase 5 builds the queue service's new files, and no build uses them. Phase 10 makes the service live in one merge; nothing runs the service beside the old checker.
- **Invariants:** admission rules live once, in `memory_admit.py`; a view prints admission's answer and repeats no budget or free arithmetic. The user's bump is a queue priority (resources Phase 26): only a showrunner sets or clears the one `QueuePriority` (`priority_set`, `priority_clear`), and the queue order is `waiter_order` = `(priority_rank, since, pid)`; nothing else moves a waiting step. Every answer about the queue comes in under 1 s.

## Queue service design — the user reviews it before any code

- **Who writes it:** a queue-unit design agent, started 2026-10-30 11:3x EDT, read-only; it writes `docs/plans/builds-queue-service-design.md`.
- **What it settles:** today's measured cost of polling admission; the service (what it holds, persists, how it starts and restarts); the socket protocol; admission rules kept in one place; the commands (view, the user's override, `/showrunner:bump` priority, width, release, build log rows); the rule when the service is down; performance targets; tests; and a phased migration from today's polling admission, ordered around resources-unit's in-flight phases and Phase 1.
- **Gate:** queue-unit reviews the draft, sends it to the showrunner, and the showrunner puts it before the user. No service code until the user approves.
- **Approved:** the user, 2026-10-30, at `93c86342` ("yes i want the build queue", relayed by the builds showrunner).
- **After approval:** the design's migration phases become Phases 4+ of this plan through `/plan:to_phased_plan`, each with a Work Order, and go to the showrunner for approval before they run.

## Phases

### Phase 1 — /build:queue answers in under a second, one line per step, and says why each step waits  · status: done

#### As-built

- `build_queue.py [--columns N] [--json] [<selector>]` and `build_queue.py bump <selector>`. No word prints the view, one word prints the card, `bump` keeps its refusal (`memory_admit.UnmatchedWaiter` on stdout, exit 3); read errors exit 2. `--columns` defaults to a positive `COLUMNS`, else the stdout terminal width, else 100.
- The read path reads the queue once (`memory_admit.locked_queue_read()`), then `head_facts.read_head_facts`, then one `unit_labels.unit_labels_for(ids, os.environ)` call with every shown id (running, waiting, `head_facts.session_ids`). No full roster build, no thread pool, no `Roster:` problems line. Live: 0.4–1.2 s at load 100–135, against 4.7 s before.
- One line form: `queue_lines.Span(text, tone, bold)`, `Tone = Literal["default", "success", "error", "warning", "inactive"]`, `Line = tuple[Span, ...]`. Text output is `plain(lines)` (no Markdown table); `--json` is `json_form(lines, columns, as_of)` → `{"columns", "as_of", "lines": [[{"text", "tone", "bold"}]]}`.
- `queue_view.render_view(model: QueueModel, columns)` (Mock A): header with the head's state, waiting count per production, running rows, move lines (`↑ #<n> moved up: from #<old> by <actor> at HH:MM`, at most 5), one row per waiting step with `↑<old place>` and a `starts` estimate, the `#1 waits on` ✓/✗ block from `HeadExplanation` through one `check_lines` function, and the rest of the queue said once (`rest_lines`, `reason_phrase`). Width rules: 80–99 drop the production part, under 80 drop `starts`, then cut the name tail with `…`; a step never becomes a record.
- `queue_card.render_selection(model, selector, columns)` (Mock B): `select_waiters` matches shown name, then waiter rows; one card per matched waiting step whose first line answers `Next?`; a running match prints one `runs now` line; no match prints `No waiting or running step matches <selector>.` (exit 0).
- `head_facts` reads bump rows back to the oldest waiter's `since` as `BumpRecord`; a row with `bump_order < 0` shows its earliest move's `old_position`. `read_pace(now)` counts this host's `admission/anon_peaks.jsonl` lines from the last 60 min; place `p` starts in `round(p * 60 / pace)` min, at least 1; no pace prints `—` and the footer `starts: no step ended in the last hour`.
- The pane (`queue.tsx`) only paints one `Text` per span; `register.tsx` runs `--json --columns <n> [selector]` and asks one forced refresh when the pane width differs from `data.columns`. `QueuePaneState` keeps `selector: string | null` in the loading, queue and failed states, so a reload keeps the card.

**Files:**
- `scripts/build_queue/build_queue.py` — CLI words, `--columns`, `--json`, the one read and label call
- `scripts/build_queue/queue_lines.py` — spans, its own cell width, pad, `cut_tail`, `clip`, `wrap`, `merge_spans`, `plain`, `json_form`
- `scripts/build_queue/queue_view.py` — `QueueModel`, `step_checks`, `ChecksNotRecorded`, `step_labels`, `render_view`
- `scripts/build_queue/queue_card.py` — `select_waiters`, `render_card`, `render_selection`
- `scripts/build_queue/head_facts.py` — `BumpRecord` scan, `moves_by_step`, `queue_moves`, `read_pace`
- `scripts/build_queue/queue_fixture.py`, `golden/{view_80,view_100,view_140,card_1,card_4}.txt` — fixed 10:46 queue and byte-exact renders
- `scripts/message/unit_labels.py` — `UnitLabels`, `unit_labels_for` and file-only helpers; `roster.py` imports them
- `scripts/production/unit_marks.py` — session unit marks moved out of `unit_lookup.py`
- `scripts/herdr/place.py` — `HerdrPlace`, no herdr import; `herdr.py` imports it
- `mods/unit-board/hooks/queue.tsx`, `hooks/register.tsx`, `types/queue.d.ts` — pane paint, run args, width refresh, `QueueLines` type
- `mods/unit-board/tests/kit.ts` — one drawing inspector shared by queue and accounts tests
- `commands/build/queue.md` — runs the script, prints output in a `text` block; moving a step up is the user's, through `/showrunner:bump`

**Binds later work:**
- `QueueLines` is the one line form for text, JSON and pane; new output goes through `Span`/`Line`, never a second renderer.
- `queue_view.step_checks` returns `ChecksNotRecorded` for every step behind #1, so those steps print no budget or free-memory line. The lock-free queue snapshot work replaces it with `LineWaiter` checks and also brings `queue_read`, `match_waiters` and `queue.json`; none exists yet.
- The pane `selector` survives reloads in pane state; the selection-union work renames it to a `QueueSelection` union and converts the old saved form at load.
- `unit_labels_for` loads no herdr, role_map or design_roles (`unit_marks.py` split from `unit_lookup.py`, `place.py` from `herdr.py`, a lazy `unit_lookup` import in `production_units.py`); `test_unit_labels.py` holds this.
- `--json` stays at most 3 times the text size (Phase 1b).

**Gotchas:**
- `build_queue.py` and `build_why.py` still take the admission lock through `locked_queue_read()`; 3 of 12 live runs went over 1 s until the snapshot work lands.
- `build_why.py` keeps its own `_terminal_width`; `queue_lines.terminal_width` is a second cell width until the snapshot work joins them.
- `queueColumns` and `queueWidthRefresh` are module-level in `register.tsx`, not pane state: a mod reload resets them to 100 and idle.
- The mod runs the installed `~/.claude/scripts`; a live pane check works only after the change is on main.
- A fresh-checkout `tsc` needs the engine-written types folder copied in.

**Ruled out:** a `--wide` mode (the card holds every fact of one step); Markdown table output (Claude Code turns it into records); the full roster build on the read path; a 7-day rollback path (user said no).

### Phase 1b — The queue view renders each row once, aligns past place 99, and sends whole spans  · status: done

#### As-built

- `head_facts.HeadFacts.moves_by_step` indexes bump records once per model by `MoveIdentity`; `queue_view.move_lines`, `move_mark` and the card reuse each row's moves. A render with 5,000 unrelated records stays within 10% of one with none.
- `queue_view.step_labels` builds its fallback name only when the label is missing.
- In the card's steps-ahead list, a moved step with no move record that joined after the selected step says `joined HH:MM, N min after it`.
- The view sizes its place column from the largest shown place (`max(2, len(str(len(waiting))))`), so names align at #99 and #100.
- `queue_lines.wrap` merges neighbouring spans of equal tone and bold (`merge_spans`) as each row closes; every span keeps its `tone` and `bold` keys.

**Binds later work:** live `build_queue.py --json` stays at most 3 times the text size (2.3–2.6 times now; one span per word was 9.4). The five golden renders did not change.

### Phase 2 — /build:queue shows a budget bar and one row per waiting step, with a size bar for each  · status: done

#### As-built

- `/build:queue` with no selector prints `Build queue · HH:MM`, a budget bar (`free <F> │ taken <T>`, one cell per GiB, `T` = `QueueMemoryTotals.charged`), then one row per waiting step: place, `▸` mark, showrunner, unit (showrunner prefix removed), step, minutes waited, an amber size bar on the budget's scale and the need in GiB. It shows no running-step, move or start-estimate lines. The drawn page `docs/plans/builds-queue-view-boards.html` is the visual spec.
- Bars use whole `█` cells and the nearest eighth block; all bars share one rounding; a need over the budget draws the budget's width. Plain form (inline, `queue.md` text block) draws free as `░` and taken as `█`; one flag picks plain or colour, and `plain()` and `json_form()` read the same lines.
- Every line fits `columns`. Above 60 columns the name cells grow to the longest name; when a line still passes the width, the showrunner and unit cells of all rows shrink together (to 3 cells minimum), then `queue_lines.clip` cuts. All bars start at one column (36 at 60 columns with the room suffix).
- Row 1 state: `↑ ≤N.N mm:ss` (strict, need below budget, N = `strict_room`), `⊘ mm:ss` (strict, room ≤ 0), `↑ mm:ss` (backfill; the strict window starts at its end), `↑` with no countdown (`WindowNotStarted`), `⊘` with no countdown (head priority, first window start not written). Red hold words follow: `low memory`, `pressure` or `no reading`; a budget wait gets none.
- `--json` keeps `QueueLines`; spans add `background` and, on the countdown, `until` (epoch seconds). The pane redraws the countdown each second from `until − now` with no script run; the 10 s refresh stays. `--you <session id>` marks that session's rows; inline falls back to `caller()`'s id.
- `BumpOff` text is `bump is off; nothing can move a waiting step`; the CLI prints it on stdout and exits 3.

**Files:**
- `scripts/lint/memory_admit.py` — `strict_room`, `HeadExplanation.order` forms, `HeadAdmissionStatus`, pressure-aware `LineWaiter.fits_now`, `BumpOff` text, `older_fit_text`
- `scripts/build_queue/queue_view.py` — `render_view`, budget bar, rows, size bars, `head_status`
- `scripts/build_queue/queue_lines.py` — `Span` (defaults, `background`, `until`), sparse `json_form`, `suggestion` tone
- `scripts/build_queue/head_facts.py` — clock, budget, head need, `window_clause`; handles `WindowNotStarted` before any deadline read
- `scripts/build_queue/queue_card.py` — `order_check` with `BehindOlderFit`
- `scripts/build_queue/build_queue.py` — CLI, `--you`, plain/colour flag, bump refusal
- `scripts/build_queue/queue_fixture.py`, `scripts/build_queue/golden/view_{60,80,100,140}.txt` — fixed queue and plain renders
- `mods/unit-board/hooks/register.tsx`, `mods/unit-board/hooks/queue.tsx`, `mods/unit-board/types/queue.d.ts` — pane paint, countdown, timer
- `mods/unit-board/tests/queue.test.ts`, `mods/unit-board/tests/kit.ts` — pane tests and the shared session fixture
- `commands/build/queue.md`, `docs/as-built/build-targets.md`, `docs/as-built/unit-board.md`, `docs/as-built/build-memory-admission.md` — docs

**Binds later work:** `memory_admit.strict_room(budget, head_need) -> int` is the one room formula: `HeadRoom.room`, `window_clause` and the view's row 1 call it. `HeadExplanation.order` is `StrictUntil | BackfillUntil | WindowNotStarted`; admission (`head_window`, `queue_decision`) does not change. `LineWaiter.fits_now` is true only when budget and free checks pass and no pressure hold applies; queue order is separate. `queue_view.head_status` reads low memory from `HeadAdmissionStatus.decision.free_passes`. `queue_card.order_check` prints `BehindOlderFit` through `older_fit_text`, the one source of that text. Sparse `json_form` omits every span field at its `queue_lines.Span` default, and the pane reads a missing field as the default. The pane timer's generation guard in `register.tsx` stops a close during `await $.ui.panes()` from starting a timer. The `kit.ts` session fixture is the one session setup the queue pane tests use. The queue snapshot keeps every row 1 state identical and carries every input these read.

**Gotchas:**
- `HeldBehindOlderFit` holds a younger step whenever an older step fits, so strict-room tests need a budget no older step fits (60 GiB in the fixture).
- Python and TypeScript each format `mm:ss`; the countdown width changes at 10:00 → 9:59.
- A countdown span that the width cuts loses `until`, so the pane shows the cut text as sent.

**Ruled out:** JSON at 9x the plain text (sparse defaults keep it within 4x); a tone on a blank mark cell (blank cells join the next span); `⊘` for every strict head (a strict head with room lets younger steps start).

### Phase 3 — /build:queue reads a lock-free snapshot and answers in under a second every time  · status: done

#### As-built

- `memory_admit.gather_queue_inputs(directory, processes, now, memory={}, *, readings=READING_NOT_TAKEN) -> QueueInputs` does every file, `/proc` and environment read; the pure `queue_read_from(inputs) -> QueueRead` reads none. `QueueInputs` holds `ordered`, `running`, `reservations`, `host`, `pressure`, raw `recovery: RecoverySpacing`, `limits: AdmissionLimits` (resolved once by `resolve_admission_limits`, the one resolver that `decide` also calls), `override` and `now`. `read_queue_pass`, the locked read and the snapshot run this one decision path.
- `exclusive_queue(directory)` yields `QueueSnapshotReadings` and, in its finally block, publishes `admission/queue.json` (`{"v": 1, "written_at", "inputs"}`) through `write_queue_snapshot` (`queue.json.tmp` + `os.replace`, no fsync). Every exclusive section, `admit_head` included, publishes this way, also on an exception path; a failed write never changes the section's result. A bump takes no lock and writes no snapshot.
- `queue_read() -> QueueRead | QueueBusy` uses the snapshot when `0 <= age <= SNAPSHOT_MAX_AGE_SECONDS` (10 s). Else it calls `locked_queue_read(timeout=QUEUE_READ_LOCK_SECONDS)` (0.5 s). On timeout, a parsed but old snapshot gives `QueueRead.freshness = StaleReading(written_at)` (the view header time prints in the warning tone); a missing or bad file gives `QueueBusy`. `build_queue.py` (view, card, `--json`) and `build_why.py` print `queue busy; try again` on `QueueBusy` and exit 0.
- `decision_checks(decision, need, available) -> tuple[BudgetCheck, FreeCheck]` is the only check constructor; `LineWaiter` carries `decision`, `budget` and `free`, so every card prints its own pressure, budget and free lines. `fits_now` stays the step's own resource fit.
- `step_matches(selector, step)` is the one identity rule (session id, recorded name, folder, folder name); `match_waiters(selector, view)` takes a place or an identity. `reason_phrase(reason, waiting)` gives `behind #<k>, an older step that fits`, or `behind an older step that fits` when no waiting step matches.
- The pane holds `QueueSelection = { kind: 'view' } | { kind: 'card'; selector: string }`; `register.tsx` converts an older string or null `selector` at load.

**Files:**
- `scripts/lint/memory_admit.py` — inputs, pure read, snapshot codec, `queue_read`, `exclusive_queue`, `resolve_admission_limits`, `decision_checks`, matchers
- `scripts/build_queue/build_queue.py`, `build_why.py` — call `queue_read()`; print the `QueueBusy` line
- `scripts/build_queue/queue_view.py`, `queue_card.py` — checks for every step, stale header time, `BehindOlderFit` place phrase, selection through the matchers
- `scripts/build_queue/queue_fixture.py`, `golden/card_4.txt` — fixed 10:46 queue as `QueueInputs`; card #4 with its checks
- `scripts/lib/terminal_cells.py` — `terminal_width`, the one cell width (also used by `queue_lines.py` and `scripts/production/dailies_render.py`)
- `mods/unit-board/types/queue.d.ts`, `hooks/register.tsx` — `QueueSelection` and the load conversion

**Binds later work:** "One decision pass…", "The queue service runs beside…" and "Every build, view and command uses the queue service…" build on: `queue.json`, its writer (published in the exclusive_queue finally block); `queue_read`'s snapshot path (snapshot used when 0 ≤ age ≤ 10 s, else a locked read with 0.5 s timeout); `StaleReading` and `QueueBusy` ("queue busy; try again", exit 0); `queue_read()` as the one entry point of the view, the card and build-why; the `QueueInputs` serial form (`snapshot_fields`, `snapshot_value`, `parse_snapshot_value`); `queue_read_from`; `gather_queue_inputs`; `decision_checks`; `resolve_admission_limits`; the card and renderer reading `QueueRead`; the pane's `QueueSelection`; `scripts/lib/terminal_cells.py` `terminal_width`.

**Gotchas:**
- The snapshot codec must encode and parse every type in `QueueInputs`. A type it cannot parse makes every read fall back to the locked read without an error. Each new input type needs a round-trip test and a held-lock test that proves the snapshot path.
- A snapshot whose non-compiler running steps do not match its reservation count reads as absent. Legacy reservation rows get `test_threads` and `call_id` set to `None`.
- Importing `roster` from `build_why.py` adds about 80 ms; import the label reader from `scripts.message.unit_labels`.

### Phase 4 — One decision pass decides for every waiting step, and the check and the queue read both call it  · status: done

#### As-built

- `admission_pass(inputs: QueueInputs, now: float) -> PassResult` in `memory_admit.py` is the one decision for every waiting row. It is pure: no file, `/proc`, environment or clock read, and no write. It calls `admission_walk` once for each input scenario and reads every row's decision from that one walk. A running step that ends is its own scenario: `inputs_after_steps_end(inputs, ended)` makes the changed copy, and one walk runs on it. `queue_decision` and `reservation_blocker` are gone.
- `PassResult` holds `rows: tuple[PassRow, ...]` in queue order, `head: HeadPriority | NoHeadPriority`, `totals: AdmissionDecision` (`decide(available, total, 0, memory)`), `recovery: RecoverySpacing`, `head_marks: tuple[HeadMark, ...]` and `override: OverrideKept | OverrideEnded`. `PassRow` holds `owner` (`<pid>-<start_time>`, from `waiter_owner`), `decision`, `reason`, `blocker: BlockingIndex | NoSingleBlocker | BlockerNotRequested` and `admission: ProposedAdmission | NotAdmitted`. `ProposedAdmission` holds `rank`, `recovery` and `override`. A `HeadMark(path, waiter)` sets `holds_younger` and `priority_since` on a head that has no `priority_since`. `OverrideEnded.action` is `"admitted"` or `"lapsed"`.
- Rules of the pass: each admitted row charges its need to the rows after it. In a recovery window (`RecoveryStarts`) at most one row admits; its proposal sets `next_start_at = now + RECOVERY_START_SECONDS`, and `PassResult.recovery` keeps the start due. `next_recovery(recovery, pressure, now)` is the one recovery change. `estimate_expired(waiter, now)` is the one expiry rule (estimate `HEAD_SECONDS` or more old): the row holds with `EstimateExpired(estimated_at)`, a `WaitReason`, and keeps its place and its room. A `ReadingUnavailable` host holds every row.
- Proposed, not committed. `check` builds `QueueInputs` with `gather_queue_inputs` from the readings it already took, adds its own waiter row, sets `blockers_for` to its own owner, calls the pass and commits only its own row. `commit_pass_effects` writes its proposal's `recovery` and `override`, or `PassResult.recovery` and `PassResult.override` when its row does not admit, before the reservation write. `check` writes the head marks and prints `memory admission: estimate expired while waiting for the lock; refresh on the next poll` when its own row expires. A launch with no ledger (no `admission/` folder) decides alone through `decide()`.
- `QueueInputs.blockers_for: frozenset[str]` names the owners that get a blocker; every other row has `BlockerNotRequested`. `blocking_index(owner, endings)` finds the one running step whose end admits the row. `blocker_identity(index, reservations, processes, proc) -> Blocker` resolves it at the process and sidecar boundary, for `check`, `blocker_snapshot` and `current_blocker`. The snapshot codec (`snapshot_value`, `parse_snapshot_value`) writes a `frozenset` as a sorted list.
- `QueueExplanation.decided_from: QueueInputs` holds the inputs of its pass. `queue_read_from` runs the pass once. `head_after_steps_end` and `smallest_head_release` run it on `inputs_after_steps_end(explanation.decided_from, ended)`. `released_resident_bytes(running, ended)` is the one calculation of freed memory (measured bytes less swap). `HeadAfterEnd` includes `EstimateExpired`.
- Output of `EstimateExpired`: `reason_phrase` gives `estimate expired`; `head_status` ends row 1 with `estimate expired` in the error tone; the card adds `✗ estimate       expired at HH:MM; refreshes on its next poll` above its resource checks; build-why's head cause is `estimate refresh: the estimate expired at HH:MM; it refreshes on the next poll`, and its release line is the same text without the `estimate refresh:` prefix (`estimate_refresh_text`). The card's `↑` move mark and `queue_view.is_moved` are gone. `build_why.py` reads labels from `scripts.message.unit_labels`, not `roster`.

**Files:**
- `scripts/lint/memory_admit.py` — the pass and its types, `next_recovery`, `estimate_expired`, `commit_pass_effects`, `blocking_index`, `blocker_identity`, `inputs_after_steps_end`, `released_resident_bytes`, `QueueExplanation.decided_from`, the `frozenset` codec
- `scripts/lint/test_memory_admit.py` — the pass tests: check and view agree, one walk per scenario, proposed against committed, codec round-trip, release helpers
- `scripts/build_queue/queue_view.py`, `queue_card.py`, `build_why.py` — the `EstimateExpired` output; no move mark; the `unit_labels` import
- `scripts/build_queue/queue_fixture.py`, `golden/card_4.txt` — the fixture carries `decided_from`; card #4 has no `↑` marks
- `scripts/build_queue/test_build_queue.py`, `test_build_why.py`, `test_queue_card.py`, `test_queue_view.py` — the new reason and its 60-column pane form
- `docs/as-built/build-memory-admission.md` — the "One decision pass" paragraph

**Binds later work:** "Every build, view and command uses the queue service…" commits a `ProposedAdmission`'s `recovery` and `override` only for an admission it commits, and else writes `PassResult.recovery` and `PassResult.override`. Its release helpers read `decided_from`. The no-ledger launch rule is held by `test_aged_launch_without_a_ledger_keeps_its_own_memory_checks` until `check` goes.

**Gotchas:**
- Resources Phase 26's queue priority record is not in the code. Waiter order and priority come from `since`, `bump_order`, `holds_younger` and `priority_since`; `queue_view.has_move_priority` reads `bump_order < 0`, and `head_facts.py` is unchanged.
- A test that moves its simulated clock `HEAD_SECONDS` past a persisted `estimated_at` tests expiry, not the window. Refresh the estimate before the clock moves.
- Blocker scenarios cost one walk for each running step. The pass makes them only when a requested row holds.

**Ruled out:** a second inputs type beside `QueueInputs`; a `queue_decision` call per row (a full walk to read one row).

### Phase 4b — The build budget is a config value, 24 GiB for a one-day trial  · status: done

As built (fe2f920a, on origin/builds cc16b3fe, 2026-10-31): `[memory] build_budget_gib=24` in `config/lint.conf`. One reader, `memory_admit.build_budget_bytes()`: `BUILDLOG_BUILD_BUDGET_GIB` wins, then the config value, then `FALLBACK_BUILD_BUDGET_BYTES` (21 GiB) for a missing or invalid value; read at each check. `head_facts`, `queue_view`, `build_why`, `AdmissionDecision` and the expected-peak cap call it. Tests pin the budget by env. Showrunner ask; the user approved 24 for a one-day trial.

### Phase 4c — The queue snapshot no longer stores the build budget; every reader resolves it live  · status: done

As built (b20effdf, on origin/builds e2773be6, 2026-10-31; builds showrunner, urgent): a `verify.sh lint` in a worktree from before Phase 4b ran its own `memory_admit.py` and wrote budget 21 into `admission/queue.json`, so `/build:queue` showed 21.0 or 24.0 at random. `QueueInputs` has no `limits`; `queue_read_from(inputs, limits)` takes them from its caller, and the private `_queue_read_live` resolves them once per reading for `queue_read` and `read_queue_pass`. The snapshot is version 2; a version-1 snapshot reads as absent and the reader takes the bounded locked read. A check from an old tree still decides its own step with its own budget until that tree merges builds.

### Phase 4d — Phase 4 carries resources Phase 26: merge of builds at e2773be6  · status: done

#### As-built

`origin/builds` at e2773be6 is merged into this branch, so Phase 4's one decision pass carries resources Phase 26, one copy of each rule. No `head_since`: waiter order is `(priority_rank, since, pid)`, and `priority_rank == 0` marks the bumped unit (it replaces `bump_order`). `/build:queue bump <unit>` runs `memory_admit.run_priority(["set", ...])` (`priority set`), which prints `queue priority set for <unit>: <result>`; `bump_waiter` is gone. During a token wait the waiter row stays; `token_wait` records `token_left_at`, and the token gap appends under the admission lock with a duplicate check, so a retry after a stop adds no second row. `head_facts.py` and `queue_view.py` read `priority_rank`; the view and card goldens follow.

**Files:**
- `scripts/lint/memory_admit.py` — `priority_rank` order, `priority set`, token-wait row and gap
- `scripts/build_queue/build_queue.py` — `bump <unit>` runs `priority set`
- `scripts/build_queue/head_facts.py`, `queue_view.py`, `golden/` — the priority read and the goldens

### Phase 4e — unit_marks.py is session_staff.py, and rca reads the one showrunner mark  · status: done

#### As-built

`scripts/production/session_staff.py` (was `unit_marks.py`, no wrapper and no old name) matches a running session to its staff row through its session labels (the unit name and staff id a session carries), and holds the director lookup too, because one caller uses both. It loads no herdr module. `scripts/message/rca.py` imports `PRODUCTION_MARK` from it and keeps no copy. Every importer (`herdr.py`, `unit_labels.py`, `unit_lookup.py`, `rename_unit.py`, `stall_watch.py`) and its tests use the new name; `docs/as-built/herdr-adoption.md` and `session-roster.md` say "Matching a running session to its staff row". `git grep unit_marks` finds only this plan.

**Files:**
- `scripts/production/session_staff.py` — the session-to-staff-row match and the director lookup
- `scripts/message/rca.py` — imports `PRODUCTION_MARK`

### Phase 4f — builds at a546fe66: Phase 4 and the rename carry the live-budget view fix  · status: done
**Depends on:** Phase 4 and Phase 4e.
The builds showrunner asked (2026-10-31): merge `origin/builds` (a546fe66, the view fix b20effdf) into this branch and send one notice for Phase 4 and Phase 4e. Two merges meet: Phase 4's `admission_pass(inputs, now)` reads `inputs.limits`, and the view fix removed `limits` from `QueueInputs` (the reader resolves them live and passes them to `queue_read_from(inputs, limits)`). The second to land (this branch) carries the view fix's state: no budget in `QueueInputs` or the snapshot, limits passed in. `unit_lookup.py`: keep this branch's name `observed_staff_marks` with the builds side's `running` parameter.

### Phase 4g — memory_admit.py's admission limits are config values in config/lint.conf, read at use  · status: done
**Depends on:** Phase 4f (the reader resolves limits live and passes them in; the snapshot holds no budget).

#### As-built

- The admission timing (ramp, head, strict, backfill, recovery spacing), the pressure limits and the peak hold are `[memory]` keys in `config/lint.conf`. `peak_hold_seconds` is the default hold, and `peak_hold_seconds.<kind>` sets one step kind's hold (`merge-gate` holds 180 s).
- One reader: `read_memory_config() -> MemoryConfig(policy, test_phase_margin, build_budget_bytes, fallbacks)` is silent (the `check` path); `memory_config()` prints each `ConfigFallback` once per process on stderr (queue views, build-why, priority, the merge gate). A missing or invalid value, an overflowing `build_budget_gib` included, uses its default.
- `AdmissionPolicy(timing, pressure, peak_hold: PeakHold)` is frozen. Every rule takes it as a required argument: `admission_pass(inputs, now, limits, policy)`, `queue_read_from(inputs, limits, policy)`, `step_promise(row, now, reading, policy)`, `estimate_expired(waiter, now, policy)`, `build_why.charge_text(basis, zone, policy, step)`. No module constant for these values exists.
- `QueueExplanation` keeps the policy it was decided by, so its counterfactuals do not change with a later edit.
- `queue.json` v4 is `{"v": 4, "written_at", "policy", "inputs"}`. `queue_read()` reads the config once, uses the snapshot only when its policy equals the live one, else takes the bounded live read (`_queue_read_live(inputs, config)`); a lock timeout answers `QueueBusy` unless the snapshot is compatible.
- The queue service passes the pass's `MemoryConfig` into test-phase estimation.
- One config read costs median 0.035 ms.

**Files:**
- `scripts/lint/memory_admit.py` — the reader, `MemoryConfig`, `ConfigFallback`, `AdmissionPolicy`, `PeakHold`, the policy-taking rules, the v4 snapshot
- `config/lint.conf` — the `[memory]` keys
- `scripts/build_queue/build_why.py` — renders the hold of the row's step kind
- `scripts/build_queue/admission_service.py` — passes the config into estimation
- `scripts/build_queue/admission_test_support.py` — `transient_scope` passes `--expand-environment=no`
- `docs/as-built/build-memory-admission.md` — the config contract

**Binds later work:** the policy is a required argument of every rule; a projection never joins old evidence with a newly read policy. The `ConfigFallback` line is owned by the client-facing commands, and protocol output carries no warning text.

**Gotchas:** systemd-run v254 and later expands `$$` in its arguments; a scope fixture without `--expand-environment=no` makes the scope test always skip.

**Ruled out:** a queue cache that serves evidence under a policy other than the one it was captured under.

### Phase 4h — A queue place helper lets a non-cargo program wait for memory in the build queue  · status: done
**Depends on:** Phase 4g (its `[memory]` reader gives each program's first peak).

#### As-built

- `scripts/build_queue/queue_place.py`: `queue_place(program, repo, *, sizing=CONFIGURED_FIRST_PEAK, before_check=no_hold_wait)` is a context manager that yields `QueuePlace`. It waits for admission through `memory_admit.check` (each turn calls `before_check()` first), prints `<program> memory wait: <GiB> needed; <reasons>` on stderr on a change or once a minute, and releases on every exit (`memory_admit.release`, `cancel_waiter`, and any reservation published with its sidecar).
- `QueuePlace.popen(argv, **popen_arguments)` starts one program per place. Scoped mode starts it in a transient `builds.slice` scope through the `--scope-start` wrapper: the wrapper publishes the scope's cgroup in the sidecar and checks the holder (pid plus start time) before and after the publish, then execs. Unscoped mode (`BUILDLOG_SCOPE=0`, holder already in `builds.slice`, failed probe) samples the family with `ProgramTree` (ppid walk from seen pids plus the process group).
- `QueuePlace.observe(peak_bytes, current_bytes)` writes `.max` and `.wave`; one sampler thread per program reads the scope's `anon`. `finalize_reservation` writes the one `anon_peaks.jsonl` row per `reservation_id`.
- Exit: `status` is the program's exit code, 1 on an exception. A program still running at exit gets SIGTERM, then SIGKILL after `program_stop_seconds`, before the release. The place installs its SIGTERM/SIGINT handler only where the caller has the default handler; a caller's own handler (the merge gate's) stays. `defer_start_signals` and `defer_cleanup_signals` guard start and cleanup.
- `memory_admit.py`: `ProgramProfile(program, repo, worktree, sizing: FixedCapSizing | HistoryPeakSizing)`; `HistoryPeakSizing(first_peak_bytes, estimate: MeasuredProgramPeak | FirstProgramPeak, estimated_at)` refreshes every `head_seconds`; `FixedCapSizing(cap_bytes)` never estimates. `program_peak_estimate(program, host, now, root, first_peak_bytes)` is the p90 of the host's rows for that program. `MemoryConfig.program_first_peaks` reads `program_first_peak_gib.<program>` (`rust-analyzer=18`). An invalid first peak raises `ProgramSizeUnavailable`; no first peak and no sizing raises `QueuePlaceRefused`.
- `merge_gate.gate_reservation` keeps its signature and calls `queue_place("merge-gate", repo, sizing=HistoryPeakSizing(cap, FirstProgramPeak(cap), 0.0), before_check=hold.wait_until_free)`. `suite_scope_prefix` and `defer_cleanup_signals` live in `queue_place.py`; `merge_gate.py` and `style_plan.py` import them from there.

**Files:**
- `scripts/build_queue/queue_place.py` — `queue_place`, `QueuePlace`, the wait loop, the scope prefix and wrapper, `ProgramTree`, the sampler, the signal guards
- `scripts/build_queue/test_queue_place.py` — scope, exit-kind, sizing, warning and fixed-cap tests
- `scripts/lint/memory_admit.py` — `ProgramProfile`, the two sizings, `program_peak_estimate`, `program_first_peaks`, `ProgramSizeUnavailable`
- `scripts/build_queue/admission_protocol.py` — the `Profile` union carries both sizings
- `scripts/production/merge_gate.py` — `gate_reservation` on `queue_place`
- `config/lint.conf` — `program_first_peak_gib.rust-analyzer=18`
- `docs/as-built/build-memory-admission.md` — the queue place helper and the two sizings

**Binds later work:** the merge gate's sizing is the one `sizing=` argument of `gate_reservation` (The merge gate holds its whole memory cap in the admission queue). `queue_place` calls `memory_admit.check` and `release` directly and moves to the service client with the other callers (Every build, view and command uses the queue service).

**Gotchas:** snapshot ancestry cannot hold a program family without a scope: a `setsid()` descendant orphaned between two samples escapes `ProgramTree`. A place that replaces the caller's signal handlers breaks the merge gate's stop (exit 143). Process-group signals in scripts and tests go through `scripts.lib.process_group.send_process_group`; `scripts/lib/test_process_group.py` checks every file.

**Ruled out:** a `PR_SET_CHILD_SUBREAPER` reaper wrapper for the unscoped path (it needs an ECHILD-confirmed completion message and a new Popen contract).

### Phase 4i — A queue place with no memory scope stops a detached descendant before release  · status: done
**Depends on:** Phase 4h (`queue_place.py`)

#### As-built

- `QueuePlace.popen` always starts the program in its own transient scope under `builds.slice`, nested when the holder already runs there (builds showrunner, 2026-11-01: option B). With no scope it raises `QueuePlaceRefused("the queued program requires a memory scope; set BUILDLOG_SCOPE=0 only for tests")` before the fork. `probe_scope_prefix()` is split out; `suite_scope_prefix()` keeps its output for `merge_gate.run_suites` and `style_plan.split_all`.
- `StartedProgram.tracking` is `ScopedProgram | ProgramTree`. `ScopedProgram` samples the scope's `cgroup.procs` (`program_members`, nested scopes included). `ProgramTree` runs only with `BUILDLOG_SCOPE=0`, for tests, and is outside the guarantee.
- `finish()` calls `stop_scope(scope, deadline, sample_seconds)`: it rereads the scope's `cgroup.procs` until it is empty, sends TERM until `program_stop_seconds`, then writes `cgroup.kill` (per-pid SIGKILL where the kernel has no `cgroup.kill`). The release runs only after the scope is empty. The leader's exit status stays the place's `status`.
- Live check on natedev: holder 55.8 MB + program 71.6 MB = charge 127.8 MB; a `setsid()` grandchild whose parent exits before the first sample is stopped before the release. No fallback.
- Callers: `merge_gate.gate_reservation` is the only `queue_place` caller; it runs on natedev, which has a systemd user manager.

**Files:**
- `scripts/build_queue/queue_place.py` — `probe_scope_prefix`, `ScopedProgram`, `scope_members`, `stop_scope`, the refusal before the fork
- `scripts/build_queue/test_queue_place.py` — refusal, nested-scope, fork-during-stop and `BUILDLOG_SCOPE=0` tests

**Binds later work:** the scope-only start, the refusal before the fork and the stop that empties the scope before release move with `queue_place` to the service (Every build, view and command uses the queue service).

**Gotchas:** one read of `cgroup.procs` is not a stop: a process can fork between the read and the signal, so the stop loops until the scope is empty.

### Phase 5 — The queue service, its socket protocol and its client exist and pass in-process tests; no build uses them yet  · status: done
**Depends on:** Phase 4

#### As-built

- `scripts/build_queue/admission_protocol.py` holds the framed socket messages, their codec and their size limits; its `Profile` union is the step and program profiles.
- `scripts/build_queue/admission_service.py` is the queue's one owner: one epoll loop holds waiters and reservations, calls `admission_pass` on each pass, promotes launches, makes one override decision per pass, and arms a reconnect grace after the listener is ready (self-bound or `LISTEN_FDS`). Reads are bounded and nonblocking; the total request size has a configured limit; a refused admission detaches its connection. `serve` requires `--state-dir`. Test-phase estimation runs on one worker thread with the pass's `MemoryConfig`.
- `scripts/build_queue/admission_client.py` sends request, wait and release, and retries inside the grace window.
- `scripts/build_queue/admission_test_support.py` holds the in-process server, the fake clock, the per-test socket under a short folder (the AF_UNIX path limit) and the fault hooks. No build uses the service.
- The service interval, grace and size values are `[memory]` keys read at use; `test_phase_need` takes a history path.

**Files:**
- `scripts/build_queue/admission_protocol.py`, `admission_service.py`, `admission_client.py`, `admission_test_support.py` — the service, its protocol, its client and its test support
- `scripts/build_queue/test_admission_protocol.py`, `test_admission_service.py`, `test_admission_client.py` — in-process tests

**Gotchas:** a long `TMPDIR` breaks AF_UNIX socket paths; tests bind under a short folder.

### Phase 8 — A nextest step's charge falls to its test-phase need when its tests start  · status: done

#### As-built

- **The mark.** `test_phase_path(row) -> Path | NoTestPhaseFile` gives `$BUILDLOG_PEAK.test-phase.json` from the reservation's `sidecar` (`.cgroup` replaced); `observed_test_phase` reads it only through `record.read_test_phase`, at run time. Admission never writes or deletes that file; `record.py` owns it. `test_phase_history(row, reading, phase)` is the one eligibility rule: an `ObservedTestPhase` window and a stored `TestPhaseNeed` give `TestPhaseStarted(started_at, peak_so_far, need)`. No file, no window, an unreadable file or no stored need keep today's `PeakSoFar` / `PeakDecayed`; an unreadable `.max` keeps `PeakFileUnreadable`. `step_promise(row, now, reading)` is split from `step_memory`.
- **N, the test-phase need.** `test_phase_need_from_rows(rows, row, host, now)` is the pure rule; `test_phase_need` feeds it from the one anonymous-history reader. Key: this host's `nextest` rows over `HISTORY_DAYS` with the same `repo_identity`, `test_scope`, `package` and sorted `filters`, no widening. Width (`test_threads`) picks the form in order `same_width`, `wider_unscaled` (nearest wider width, no scale-down), `scaled_up` (widest narrower width × width / recorded width, rounded up); N = max(p90, `FILTERED_FLOOR`). `TestPhaseNeed(bytes, runs, width, recorded_width, form, margin)` or `NoTestPhaseNeed(reason)`, reason `no_width`, `no_history` or `not_recorded`.
- **Stored outside the lock.** After `check` admits a nextest step, and in `set_width` for the new width, N is computed outside the admission lock and written by `update_reservation` (locked, atomic replace, skips a gone reservation). The field `test_phase_need` uses the one tagged codec (`_need_record` / `_read_need_record`) behind the typed entry points `need_basis_record` / `read_need_basis` and `test_phase_need_record` / `read_test_phase_need`; a missing field reads as `NoTestPhaseNeed("not_recorded")`.
- **The charge.** For `TestPhaseStarted`, `charge_basis` gives `TestPhaseCharge(bytes, peak_so_far, need, started_at)`, `bytes = ceil(max(peak_so_far, need.bytes) × need.margin)`. It does not decay; the compile `.max` and its `.wave` decay no longer count. `UsingNow`, the promise and `PeakFileUnreadable` stay; `growth_headroom` does not change. With no mark or no N the step keeps today's charge and its 1500 s decay.
- **The margin.** `test_phase_margin=1.2` in `config/lint.conf` `[memory]`, read at use by `test_phase_margin()`; an absent or bad value (not finite, below 1.0) gives `FALLBACK_TEST_PHASE_MARGIN` (1.2). The margin is frozen into the need at the need write; a need record with no margin reads with the fallback. The snapshot format is version 2 (the need carries its margin).
- **Charge-use evidence and the history row.** When a real `check` decision selects `TestPhaseCharge`, `check` writes `TestPhaseChargeUse(bytes, selected_at, reservation_id, need)` to the reservation's `.test-phase-charge.json` at its write boundary; a failed write never changes admission. `FinalizationIntent` snapshots `test_phase_peak_bytes`, `test_phase_started_at`, `test_phase_need` and the evidence before the first history append, so every retry publishes the same evidence after `record.py` has deleted the test-phase file. The finalizer copies the evidence into the `anon_peaks.jsonl` row and never infers use at cleanup.
- `build_why.charge_text` prints `tests since <HH:MM>: peak so far <GiB>, need <GiB> (<form>, p90 of <runs> runs at width <width>), × <margin>`.

**Files:**
- `scripts/lint/memory_admit.py` — mark read, need rule and codec, charge, margin reader, evidence, finalizer fields
- `scripts/lint/test_memory_admit.py` — charge, need, codec, storage, finalizer retry and evidence tests on `MemoryQueueFixture`
- `scripts/build_queue/build_why.py`, `test_build_why.py` — the `TestPhaseCharge` line
- `config/lint.conf` — `test_phase_margin`
- `docs/as-built/build-memory-admission.md` — the test-phase charge

**Binds later work:** "Every build, view and command uses the queue service…" moves the charge-use evidence write with the decision site that selects `TestPhaseCharge`, and keeps the frozen need margin. "memory_admit.py's admission limits are config values…" reads limits with the same read-at-use pattern as `test_phase_margin` and `build_budget_gib`. A reservation from "A queue place helper lets a non-cargo program wait…" or "The merge gate holds its whole memory cap…" has no test-phase need (`NoTestPhaseNeed`) and no evidence file.

**Gotchas:**
- Evidence is written only at `check`'s write boundary, outside the pure pass. Any later admission home must keep that site.
- The margin in force is the one frozen in the need, not today's config.
- Replay on 4c data: hana 2 of 22 and tool-based-ui-trunk 1 of 2 rows with N went above their charge (worst +0.397 GiB); cargo-liner 0 of 146. A 24 h check after the merge to main is owed by queue-unit; hana's margin rises by config if it is above 2.3 % on 50 or more rows with N.

**Ruled out:** `AdmissionLimits.test_phase_margin` (the applied margin could differ from the stored one); classifying charge use at cleanup time (owner-gone cleanup invented or missed evidence); widening N's rows to the package or the repository (4.3 % against 2.3 % above the charge).

### Phase 9 — The merge gate holds its whole memory cap in the admission queue  · status: done
Moved from builds-resources Phase 27 (builds showrunner, 2026-10-31): one owner for admission changes in `memory_admit.py`.
**Depends on:** devtools-unit's merge-gate checkpoint that raises the gate memory cap to 6 GiB (`a70f99e6`), which is on `main`, `origin/builds` and this branch: `merge_gate.py` :73 has `DEFAULT_MEMORY_CAP_MIB = 6144`. Phases 4g and 4h (the policy reader, and the program profiles and gate adapter this phase changes). Phase 8 (resources Phase 15; same files, `memory_admit.py` and `build_why.py`; resources 26 → Phase 8 → this phase land one after another). Merge `origin/builds` first. Global order (resources numbering): Phases 21, 16, 25, 30, 28, 4, 24 and 32 are checkpointed; serial memory chain 31 → 33 → 26 → 15 (Phase 8) → **27** (this phase) → 20 → 11 → 17 → 18 → 19 → 8; 29 runs beside the chain, checkpoints after 33 (same file, `sweep.py`) and joins before 11.

#### As-built

- `merge_gate.gate_reservation` sizes its queue place with `FixedCapSizing(cap)` from `--memory-cap-mib` (default `DEFAULT_MEMORY_CAP_MIB`, 6 GiB). The row holds the cap from the gate's start to its end, with no estimate and no refresh.
- `NeedSource` includes `"cap"`. A `FixedCapSizing` profile's check gives `ExpectedPeak(cap_bytes, "cap", 0, ProgramMemoryCap(program))`. `ProgramMemoryCap(program)` is a `NeedBasis` with the codec form `{"kind": "program_memory_cap", "program": ...}`; a `cap` source without that basis raises `ValueError`.
- `step_promise(row, now, reading, policy)` gives `LifetimePromise(row["need"])` to a row whose waiter records `need_source == "cap"`, beside a release build. The charge never decays for that row, whatever the peak hold.
- `estimate_expired` is never true for a `cap` waiter: the cap never asks for a new estimate after `head_seconds`.
- build-why: `need_text` prints "the merge gate's memory cap" for the gate and "<program>'s memory cap" for another program; `charge_text` names the step kind ("merge gate promise: holds until the step ends", "release build promise: …"). `memory_gate.sh` `buildlog_memory_line_inner` has a `cap` arm with the same text.
- `HistoryPeakSizing`, `program_peak_estimate`, `MeasuredProgramPeak` and `FirstProgramPeak` stay for the other programs.

**Files:**
- `scripts/lint/memory_admit.py` — the `cap` source and `ProgramMemoryCap` basis, the `step_promise` case, the `estimate_expired` exception
- `scripts/production/merge_gate.py` — `FixedCapSizing(cap)`
- `scripts/build_queue/build_why.py` — the cap need text, the step-kind promise text
- `scripts/lint/memory_gate.sh` — the `cap` wait-line arm
- `docs/as-built/build-memory-admission.md`, `docs/as-built/merge-gate-and-test-speed.md` — the gate row holds its cap
- Tests: `test_memory_admit.py`, `test_merge_gate_suites_admission.py`, `test_build_why.py`, `test_queue_place.py`, `test_memory_gate_wait.py`

**Binds later work:** the queue service keeps the `cap` source, the `ProgramMemoryCap` basis, the lifetime cap charge and the no-expiry rule for a `cap` waiter (Every build, view and command uses the queue service).

**Gotchas:** `step_promise` reads the cap case from the waiter's recorded `need_source`, not from the step name.

### Phase 10 — Every build, view and command uses the queue service, and the polling admission goes  · status: done
#### As-built

- The queue service (`admission_service.py`) is the one admitter. `memory_gate.sh`, `verify.sh`, `invoke.sh`, the BRP launch gate, `build_hold.py`, `merge_gate.py` and `queue_place.py` call `admission_client.py` (`admit`, `launch`, `release`, `width`, `token_wait`, `view`, `explain`, `mine`). The polling `check`, the admission lock, the file readers, `.waiting`, `.reservation` and the old `queue.json` ledger do not exist.
- The shared rules stay in `memory_admit.py` (`step_estimate` with `StepEstimate`, the pure `QueueInputs` builder, `queue_read_from_pass`); the service calls them and holds no copy. `view`, `explain` and `mine` read the stored pass.
- `admission/state.json` holds reservations, waiter order and episodes, the `QueuePriority`, the admit-head record, the recovery spacing, the replay ledger and a durable build-history generation, written through `os.replace`. A first start imports the old files once and deletes them.
- A finished build advances the build-history generation. An estimate carries the generation it read; a grant needs an equal generation, otherwise the reply is `EstimateExpired` and the client refreshes the estimate.
- A durable grant replays on reconnect with the same reply and reservation, charged once. The client keeps one receive buffer per socket across timeouts.
- Each pass writes the snapshot `admission/queue.json` through `scripts/build_queue/queue_snapshot.py` (atomic replace; a write error is ignored). AWS control reads it and refuses a start on a missing, stale or partial snapshot.
- `admission_client.py --wait-events` prints `first` and `done`. `memory_gate.sh` pipes them to `buildlog_memory_event`, and `verify.sh` records the memory phase waits in phase history.
- Each pass counts `builds.slice` memory that no reservation owns (`UnownedSliceMemory`), and a held head shows real free, reserve and need in the view and the card.
- `admission_service.py drain-check [--service]` is the drain barrier; `export-priority` exports the active priority for a rollback.

**Files:**
- `scripts/build_queue/admission_service.py` — the service, `StateIntents`, the import, the snapshot writer, the generation rule, `drain-check`, `export-priority`
- `scripts/build_queue/admission_client.py` — the client for every caller, the wait line, the wait events
- `scripts/build_queue/admission_protocol.py` — the messages; `admission_socket_path` calls the override guard
- `scripts/build_queue/queue_snapshot.py` — the one queue snapshot form, its reader and writer
- `scripts/build_queue/admission_test_support.py` — the in-process service on a per-test socket, the fake clock, the fault hooks
- `scripts/lint/memory_admit.py` — the shared rules
- `scripts/lint/memory_gate.sh`, `scripts/direct/verify.sh`, `scripts/lint/invoke.sh`, `scripts/hooks/pre-tool-use-brp-launch-gate.sh`, `scripts/build_hold/build_hold.py`, `scripts/build_hold/override_guard.py`, `scripts/build_hold/override_guard.sh`, `scripts/build_queue/queue_place.py`, `scripts/buildlog/mac_idle.py`, `scripts/buildlog/record.py` — client callers
- `scripts/build_queue/build_queue.py`, `build_why.py`, `queue_view.py`, `queue_card.py`, `commands/build/queue.md` — the view and the card on `admission_client.view` and `explain`
- `scripts/build_queue/test_admission_client_wait.py` — the wait-line regressions against the in-process service
- `docs/as-built/build-memory-admission.md` — the service, the protocol, the state file, the cut-over and rollback

**Binds later work:**
- The cut-over and rollback steps are in `docs/as-built/build-memory-admission.md` → "Startup, cut-over, and rollback". The builds showrunner runs them at the merge to main, after the user's go.
- The service owns `admission/` (`state.json`, the `queue.json` snapshot) under its state folder; `queue_snapshot.py` names `admission/queue.json`.
- The owner key is `Owner` in `admission_protocol.py` and the service. A run place keeps the `--wait-events` client path.
- The card and `build_why.py` read the service only through `admission_client.view` and `explain`.
- `[memory]` keys are read at each service pass; the snapshot writer and AWS control do not read them.

**Gotchas:**
- A test that counts grants counts distinct reservations, because a replay resends the grant.
- Tests and `verify.sh` fixtures use the private service socket, never the live one. A direct `handle_request` call in a test goes inside `fixture.loop_turn`.
- Timing tests (`test_queue_view` render time, `review_links` `test_change_page`, `codex_mesh`) can fail once at load 60-100 and pass alone.

**Ruled out:**
- A shadow run beside the old checker (user, 2026-10-31): one cut-over, no second admitter.

### Phase 10a — One function names every /tmp and /dev/shm build folder  · status: done

#### As-built

- `scripts.lib.build_folders.create_build_folder(request: BuildFolderRequest) -> Path` accepts `UniqueBuildFolder(prefix, parent)` or `StableOwnerFolder(name, parent, mode)`. Unique allocation is atomic with mode `0700`. Stable allocation preserves existing contents and permissions.
- `InheritedTemporaryParent` reads `TMPDIR` at allocation. `ExplicitBuildParent` preserves the supplied parent. `SystemTemporaryParent` selects `/tmp`. `UnitRunParent` uses `unit_runs.run_roots()`. The merge gate requires `/dev/shm` without a disk fallback.
- `FOLDER_PREFIXES` defines build names. `MKDTEMP_SUFFIX_PATTERN` defines eight-character allocator suffixes. `temporary_build_folder(request: UniqueBuildFolder) -> Generator[Path]` uses standard-library cleanup and permission repair. Callers retain ownership records and cleanup order.
- The CLI prints one path. Forms are `unique --kind KIND [--owner TEXT] [--parent PATH | --system-temp]` and `unique --prefix TEXT [--parent PATH | --system-temp]`. Stable forms are `stable --name NAME (--parent PATH | --unit-root)`, `stable --path PATH`, and `stable --run-temp RUN`. `run-temp-path RUN` only names the path.
- `scratch.new()` and `scratch.target()` retain registration and task naming. `copy_record.root()` and `copy_record.copies_root()` retain root authority. `unit_runs.RUN_TEMP_NAME` names run temporary folders. `unit_runs.activate()` maintains current and former markers.
- `memory_admit.is_fresh_test()` reads `tmp_sweep.state_dir()` at use. `tmp_sweep.TARGET_DELETES_NAME` names the shared deletion record. Private sweep roots select private copies folders. Admission state and snapshots remain outside temporary build folders.

**Files:**

- `scripts/lib/build_folders.py`, `scripts/lib/unit_runs.py`, `scripts/lib/socket_test_support.py` — naming, run paths, markers, and short socket parents.
- `scripts/production/{merge_gate,fast_track,waiting,bump_gate,ci_points}.py`, `scripts/style/{bulk_run,style_sync}.py` — temporary checkout and index callers.
- `scripts/buildlog/rust_release.py` — shared trial prefix and inherited parent; Git creates the clone.
- `scripts/direct/{verify,prepare_session,implement}.sh`, `scripts/lint/invoke.sh`, `scripts/agents/codex_mesh.py` — CLI allocations and run temporary folders.
- `scripts/hooks/{session-start-temp-root,pre-tool-use-checkpoint-doc-gate}.py` — stable session roots and temporary checkpoint folders.
- `scripts/build_queue/{queue_place,admission_test_support}.py`, `scripts/mac_test/{copy_watch,linux_runner}.py`, `scripts/lint/style_review.py` — sidecar, admission, staging, and review folders.
- `scripts/lint/{tmp_sweep,sweep,folder_audit,memory_admit}.py` — shared names, ownership classification, and sweep-state reads.
- `docs/as-built/build-memory-admission.md`, `scripts/lib/test_build_folders.py`, and adjacent tests — naming reference and cleanup coverage.

**Gotchas:** `verify.sh` prepares a missing `TMPDIR` through `invoke.sh --prepare-temp` before environment loading. Script-copy fixtures require adjacent dependencies. `test_verify_nix_shell`, `test_verify_ssh_targets`, and `test_verify_step_owner` cover these dependencies. Sweeps also accept six-character shell suffixes until 2026-12-03. The SSH promotion check uses self-contained `tempfile.TemporaryDirectory(prefix="promotion-index-")` before the Mac fetch. The assertion guard can associate a decorator with the preceding test.

**Ruled out:** Unused `RegisteredTaskFolder`, `ScratchParent`, `CopiesParent`, `RequiredMemoryParent`, `task`, and `--mode` wrappers; their existing authorities remain separate.

### Phase 10b — The /build:queue card carries the build-why facts, and build-why goes  · status: done

#### As-built

- `/build:queue <position|session|folder>` shows the full queue, one blank line, and selected cards; `mine` uses the same layout.
  One `QueueRead`, one optional `head_facts.read_head_facts(reading)`, and one label resolution supply the complete page.
- Running and Queued sections share aligned memory bars with the budget summary.
  Live runs show use against grants, including merge gates; runs without reservations show `between steps`.
  Ended runs with processes have a separate section; empty scopes remain charged but have no row.
  Staff records supply unit names; missing units use session names, and unit columns remove the showrunner prefix.
- `queue_evidence.py` formats recorded need, package, filters, width, charge basis, pressure, arithmetic, token waits, follower fit, and release advice.
  `need_text(source, *, budget: int, zone: ZoneInfo)` requires the recorded budget and an explicit zone; formatters never reinterpret replies through live configuration.
- `build_queue.main` reads optional host load and computes `smallest_head_release` once for selected pages; `release_text` formats that result.
  Admission rules remain in `memory_admit.py`; `MemoryPressure.condition` records `full` or `swap`, and both pressure formatters use that cause.
- Cards show queue arrival, time waiting, and the separate order-window deadline; unavailable optional evidence remains unknown.
  The pane retains its selection and last complete page after a failed refresh.
- `build_why.py` and its command are absent; `queue_evidence.py` owns the retained formatters, including `release_text`.

**Files:**

- `scripts/build_queue/queue_evidence.py`, `queue_card.py`, `queue_view.py`, `build_queue.py` — evidence formatters, selected cards, section layout, and page inputs.
- `scripts/build_queue/head_facts.py`, `queue_lines.py` — bounded history, shared clocks, and terminal alignment.
- `scripts/build_queue/scope_activity.py`, `admission_service.py` — process ownership, live runs, and scope evidence.
- `scripts/lint/memory_admit.py` — admission rules, recorded pressure causes, reservation matching, and release calculations.
- `scripts/build_queue/test_queue_evidence.py`, `test_queue_card.py`, `test_queue_view.py`, `test_build_queue.py`, `golden/` — formatter, selection, section, and complete-page regressions.
- `mods/unit-board/tests/queue.test.ts`, `selected_page_fixture.ts` — shared terminal/pane output and failed-refresh coverage.
- `commands/build/queue.md`, `docs/as-built/build-memory-admission.md` — command instructions and evidence ownership.

**Binds later work:** `read_pressure(host, *, policy)` uses supplied thresholds; evidence formatters use reply policy and limits, with no new admission state or history format.

**Gotchas:** Unselected pages perform no release search. History scans stop at 32 MiB, and bump details show at most eight groups. Missing step checks return `ReadingUnavailable`.

**Ruled out:** A `build-why` alias; separate `head_since` state; live configuration reads for recorded evidence.

### Phase 10c — The admission settings live in config/build_admission.conf, and the code keeps no copy of them  · status: done

#### As-built

- `config/build_admission.conf` holds every admission key (the old `config/lint.conf` `[memory]` section, with `program_first_peak_gib.cargo=12`). `memory_admit.py` keeps no default copy of a value. Measured constants (`RELEASE_HEADROOM`, `ANON_SHARE`, `FILTERED_FLOOR`, `HISTORY_DAYS`) stay in code.
- Each pass makes one `conf.read` and gets a usable or an unusable `MemoryConfig`. A recovered value is usable and warns once per process at the client boundary (`ConfigurationDiagnostic`). An unusable key stops grants with one refusal line that names the file, the key and the value.
- Release and cleanup need no config: each grant saves its cleanup timing, and the service serves releases and cleanup while the config is unusable. Old ledger grants without cleanup timing restore after an upgrade.
- The service listens with unusable config, answers views with the refusal, and watches the nearest existing config folder through `scripts/lib/inotify.py`; it re-arms the watch on each event and retries a failed watch.
- Each request carries one hash of the loaded admission code (`scripts/build_queue/admission_code.py`), so `drain_check` counts only callers that run older code. Dead and reused caller records are pruned.
- `ExplainReply` carries `ConfigurationDiagnostic` diagnostics; `ConfigFallback` is gone, with no alias.
- Tests read fixture settings through `CONF_DIR`; no test depends on the live file.

**Files:**

- `config/build_admission.conf`, `config/lint.conf` — the admission keys and their move.
- `scripts/lint/memory_admit.py` — `MemoryConfig`, one read per pass, the refusal line.
- `scripts/build_queue/admission_service.py`, `admission_client.py`, `admission_protocol.py`, `admission_code.py`, `queue_place.py`, `queue_view.py` — consumers, the config watch, the code hash, diagnostics and the refusal in the view.
- `scripts/lib/inotify.py` — the shared folder watch.
- `scripts/mac_test/loopback_proof.py` — reads `cargo_first_peak_bytes()`.
- `scripts/lint/fixtures/build_admission.conf` and the build_queue, lint, hooks and production admission tests — fixture settings through `CONF_DIR`.
- `docs/as-built/build-memory-admission.md` — the settings file and the refusal.

**Gotchas:** A step loads the admission code without an extra Python start (gate fix). `admission_service.py` `main()` leaves argparse defaults to argparse; a namespace class attribute hides the default.

**Ruled out:** Optional profile fields for a missing key; an alias for `ConfigFallback`; config reads in release or cleanup.

### Phase 10d — hana CI builds wait in the build admission queue  · status: todo
**Depends on:** Phase 10 (the admission client); Phase 10c (the CI admission reads build_admission.conf)

**Why.** Showrunner decision, 2026-11-02 (builds): hana CI on natedev ran out of memory at 13:50 EST. The Test Suite and "Linux: Compile and Production Wiring" jobs built hana at the same time inside `hana-ci.slice` (`MemoryMax=18G`), and the kernel killed a 6.7 GB rustc. CI memory sits outside the 24 GiB admission budget, so two budgets split one machine. CI runner builds go through build admission, as every other build does, so one budget holds all build memory.

**Work Order.**
1. Hook point: each building cargo call in a runner job holds one admission reservation for its lifetime, the same contract as `scripts/build_hold/cargo` for sessions. Non-building commands pass through. The cargo-tile shim stays; the admission step runs in front of the real cargo.
2. Client delivery: the runner accounts cannot read `~/.claude`. The client reaches them from the store, pinned from this repo the way `cargo-tile` is pinned from cargo-liner. No copy of client code in /etc/nixos.
3. Identity: the owner is the cargo process (pid and start time). The service runs as natepiano and must read the runner process start time in /proc (no hidepid today; prove it in a test). The queue view labels the step "hana CI · <runner> · <job>" from the runner environment (`GITHUB_JOB`, `RUNNER_NAME`), through config, not hard-coded.
4. Estimates and history: CI builds use the same history keys (repository, host, shape). The service writes their history rows. A CI kill records a failed peak like any other build.
5. Waits: a CI step waits in the queue with the normal wait output in the job log. Priority and bump stay user-only. An admission outage follows the same client rule as other builds.
6. Tests: a runner-account client admitted through the group socket in a fixture; two concurrent CI cargo calls that do not fit wait in order; the queue view shows the CI label.

**What /etc/nixos must change (natedev pushes it; no unit edits /etc/nixos):**
1. `modules/linux/development.nix` `build-admission.socket`: `SocketGroup = "build-admission"`, `SocketMode = "0660"`; add group `build-admission` with natepiano and the hana runner accounts. This changes the "runner accounts are in no group" rule in `hana-runners.nix` for this one socket; record it there.
2. `modules/linux/hana-runners.nix`: put the pinned admission cargo front on each runner's PATH ahead of the cargo-tile shim; add `ADMISSION_SOCKET` only if the path differs from the default; `BindPaths` for `/run/build-admission` if the runner units' sandbox hides `/run`.
3. `hana-ci.slice`: raise `MemoryMax` from 18G to the admission budget (24G), so admission decides and the slice only catches a wrong estimate. Keep `MemorySwapMax=2G` and the `steve-ci` slot pool (CPU slots, not memory).
4. Order: the nixos change lands only after the admission cut-over enables the socket; until then CI keeps today's slice.

**Constraints from the Phase 10 architect review (2026-11-03):**
- Estimate inputs: `step_request()` in `admission_client.py` reads config and history through the home folder of the client's account. A runner account cannot read `/home/natepiano`. The service runs as natepiano and already reads both, so the service makes the estimate for a client that sends no estimate: the client sends the repository, host, shape and owner; the service reads config and history and replies with the need it charged. The runner client package holds no config or history path. A test runs an estimate under an account with no access to `/home/natepiano`.
- Service authorization: `handle_request()` in `admission_service.py` accepts priority mutations and `admit_head` without a check of the connecting account; today only the CLI checks for a showrunner. Before the socket opens to a group, the service reads the peer account (`SO_PEERCRED`) and refuses from a runner account: priority set and clear, `admit_head`, and any release, width or token request for an owner that the runner did not register. Tests deny each of these to a runner account.
- Cargo lifetime: `scripts/build_hold/cargo` handles holds and scopes but does not take memory admission. The CI front takes one admission reservation for the life of each building cargo call, the same contract as `queue_place` (`scripts/build_queue/queue_place.py`): descendants are tracked, memory is sampled, a cancel releases, and the release happens on every exit. It covers the toolchain shims (`cargo-tile`, rustup proxies) so that a call by full path still goes through admission; state each path that cannot be covered and why.
- Owners: queue-unit owns the repository code and fixtures. natedev owns the NixOS package, the socket group, the sandbox and the slice changes.

**Constraints from the Phase 10b and 11 architect review (2026-11-03):**
- Showrunner rulings (builds, 2026-11-03): one phase. Build the fitting-pair case first: CI jobs that fit together run at the same time, because parallel builds are the user's top issue. Each item cuts to the spec and removes at least as much code as it adds where it can.
- Transport settings: the client reads `silence_seconds()` before it connects (`admission_client.py:261`), and its retry and wait output read config too. The service sends these settings in its replies, or the runner client uses fixed protocol values. Test admission, waits, outage recovery and release with every local config and history read denied.
- Scopes and sampling: `QueuePlace` makes scopes through the user manager (`queue_place.py:91`), and grants return sampling paths under the service state folder. Name the owner of scope creation for a runner account and the path by which runner peak samples reach the service. Test a cancel, a surviving descendant and a failed-peak history row across the real account boundary.
- Authorization covers Phase 11 state: run-place registration checks process identity but not the account (`admission_service.py:717`), and replay lookup comes before owner validation (`admission_service.py:1049`). Bind owner, run identity, episodes and replay access to the peer account before any lookup or change. Tests refuse another account's owner, run place and replay id.
- Service estimate request: `AdmitRequest` requires an estimated profile (`admission_protocol.py:67`), and build admission refuses a missing or stale generation (`admission_service.py:762`). Add a named request variant for a service-made estimate, not an optional profile. State its refresh, generation, replay and `NoRun` behavior. `impl` owns `admission_protocol.py`; `test` owns protocol compatibility tests, including a client from the pinned store package.
- Deployment order: the socket exists already, so "after the socket" is no gate. The runner group gets socket access, and the slice cap rises, only after the service with authorization and a compatible protocol is on main and live. natedev owns the NixOS activation and the account-level acceptance.

**Workers:** `1 writer + 1 tester`
- `impl` — the CI cargo front, the service authorization and estimate path, the client package entry; hub: `scripts/build_queue/admission_service.py`
- `test` — the runner-account fixtures, the authorization denial tests, the estimate-without-home test and the two-job ordering test

**Acceptance.** Two hana CI jobs that cannot fit together under the admission checks run one after the other, and two that fit run at the same time, shown in `/build:queue` with the CI label; no CI rustc dies at the slice cap. Change-Review: in the Build queue pane during a hana CI run, the CI step shows as "hana CI · <runner> · <job>" and waits behind a session build when memory is short.

### Phase 10e — A merge gate's grant comes from its recorded peak, not a fixed 6 GiB  · status: todo
**Why.** Showrunner decision, 2026-11-03 (builds), after the 14:18 EST queue stall: two merge gates each held 6 GiB of the 24 GiB budget while they used 0.2 to 1.5 GiB. The Work Order follows before dispatch.

**Moved here from 10c (round 6).** These items are not regressions, so 10c lands without them:
1. F014: `_read_need_record` in `scripts/lint/memory_admit.py` keeps three identical rejection returns. Make one.
2. F044: move the `struct` import in `admission_service.py` `_record_caller_version` to the module imports.

### Phase 10f — The queue names every build folder it makes and deletes it when its work ends  · status: todo
**Why.** The user, 2026-11-03, via the builds showrunner: "our build-queue is supposed to handle making sure we name things and delete them when they are completed". The naming map is a design call that the builds showrunner settles. The showrunner approved it on 2026-11-03 (map and rulings: the 10f design in the unit's phase folder), with one change: a remote mirror ends when its copies.py remote lease from verify P35 ends.

**Scope (showrunner limits, 2026-11-03).** 10f does not change the callers that verify P18 moves to copies.py (merge_gate, bump_gate, fast_track, waiting, the style runs, rust_release). Its sweep never touches copies.py standing checkouts. Two checkpoints:
1. A: `build_folder_path` is the one name rule; `build_folders.py run --owner O --name N -- CMD` makes a folder, runs CMD in it and deletes it when CMD and its descendants end; `scratch.target` uses the name rule; the scratch guard refuses a clone under a unit or session tmp root and names the command.
2. B: `build_folders.py sweep` removes a marked folder only when its owner is gone (pid, start time, boot id; a read error keeps it); `tmp_sweep` calls it; one writer makes each removal record below, also for `linux_runner` floor and trim.

**Progress (2026-11-04):** checkpoint A is on main, with the test fixes for a `CONF_DIR` that lacks `build_folders.conf` (`build_folders._settings` then reads the repository file) and config read at use in `tmp_sweep`, `style_sync` and `folder_audit`. Checkpoint B is open.

**Removal record (aws-unit ask via the builds showrunner, 2026-11-03):** each folder removal writes one record with: time, target, host, repository key, slot, folder path, bytes freed, free GiB before and after, and the reason (`floor` or `trim`). Source today: `scripts/mac_test/linux_runner.py:601-658` removes or trims a slot build folder after each step and prints only `mac_cleanup=0|1|kept` (`offload.py:765` parses it); no record names the folder or the bytes freed. aws-unit's weekly report (cold steps after a floor removal, time lost) reads this record.

### Phase 10g — The queue decides where every step runs  · status: todo
**Why.** The user, 2026-11-03: "should verify be handling this or should the build queue be handling what goes where - why is verify involved?" Showrunner ruling (builds, 2026-11-03): the queue owns placement; one owner decides where every step runs. Today `verify.sh` picks the host once at start and the queue admits memory only on natedev; at 14:04-14:17 a disk decline sent two hana tests to natedev and nothing sent them back to the free Mac.

**Outline (the Work Order follows at phase start, after verify's Phase 27 hotfix lands):**
1. A step's admission request carries its eligible hosts (natedev, the Mac, SSH slots) and each host's exclusion reason.
2. The queue service places each step on the first host that is free for it, and decides again on every pass while the step waits (Mac free, Mac disk over the floor, slot free).
3. `verify.sh` and the offload path run the step where the queue says; they make no host choice.
4. `build_targets.py` eligibility, slot reserve and the spending-guard exclusion move behind the queue decision.
5. Owners: queue-unit switches every caller; verify-unit owns `verify.sh` routing and offload today, aws-unit owns target selection; the edits are agreed with both at phase start.
6. Runs beside 10e and 10f where the file sets are disjoint.

### Phase 10p — The snapshot client accepts records from an older or a newer admission service  · status: done

#### As-built

- The snapshot record parser in `scripts/lint/memory_admit.py` ignores unknown fields and fills a missing field from its dataclass default, at every nesting level. A missing field with no default and a wrong tag are still refused. A step that waits across a service restart (older client code) reads the new records.
- The four single-field shims are gone. The `fallbacks` to `diagnostics` conversion stays because it renames a field.

**Files:** `scripts/lint/memory_admit.py`; `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_admission_service.py`, `test_build_queue.py`, `test_queue_view.py` — added, removed and missing field regressions.

**Ruled out:** A version field on each record; per-field shims.

### Phase 10i — A local cargo step takes its token first, then asks for memory once  · status: done
**Why.** Builds showrunner, 2026-11-03: fastest's clippy was admitted at 20:11 EST, gave its grant back to wait 24 min for the cargo token, then waited 5 min for memory again. Cause: `verify.sh` gives back the memory grant before a busy-token wait (the `TokenWaiting` end), then asks again. Showrunner ruling: queue owns the whole change as one phase.

**Outline:**
1. A local cargo step takes its board token first. Then it makes one memory request and holds both until it ends.
2. A step with no token makes no memory request, so no idle grant blocks the token holder.
3. Every local cargo path uses the same order, the steps after Mac routing included. Nested work reuses the held token.
4. Cancel, failed admission and Mac departure give the token back.
5. Files: `scripts/direct/verify.sh` 2833-2850, `scripts/lint/memory_gate.sh`, `test_verify_token_wait.py`, `test_verify_token_reservation.py`, `test_verify_token_memory_history.py`. verify-unit holds its edits there until queue merges.
6. Evidence and proposed tests: `token_wait_findings.md` in the 10f session folder.
7. Separate defect, sent to verify-unit: the call's own cargo token stayed held for 1 h after its sweep. verify Phase 36 fixes it; 10i builds on Phase 36 after it lands on builds.
8. Design: `design.md` in the 10i session folder, with the director rulings at its end. Showrunner approved it 2026-11-03.
9. **Removal, temporary path (showrunner condition, 2026-11-03).** The service keeps serving main's old two-pass shell only until 10i reaches main. **Trigger:** the first queue checkpoint after 10i is on origin/main. That checkpoint deletes `admission_client.token_wait` and the `token-wait` command, `TokenWaitRequest` and its codec and fixture row, the service `TokenWaiting` end and handler, `AdmitRequest.token_back_at` and the token-gap writer, and their tests. After it, one way remains.
10. **True wait cause (cargo-liner defect, 2026-11-03).** `token_wait_note` in `scripts/direct/verify.sh` reads `locks/cargo.d/holder` with `read -r ... || holder=""`. board.sh writes that file with no newline, so `read` fails and the holder is cleared; every token wait then says "it came free just now". Run 20261103T220514-274283-ecf4 (handler-unit) printed that for 35 min while its own call held the token from 22:12:06 to 22:50:14. Read the whole file, name the holder agent and pid, and say "this call holds it" when `holder_pid` is the call. A memory wait prints the memory line, never a token line. Test: a holder file with no newline names its holder.

### Phase 11 — Every later step of one verify.sh run keeps the queue place of the run's first step  · status: done

#### As-built

- Linux `verify.sh` exports `BUILDLOG_RUN=<pid>:<start_time>` after detachment through `step_record.py start <pid>`. Nested calls retain it. Darwin reads no `/proc` and uses `NoRun`.
- `AdmitRequest.run` uses `RunIdentity(pid: int, start_time: str) | NoRun`, with shared `NO_RUN`. `BUILDLOG_RUN` is parsed once; missing or malformed values use `NoRun`. Older requests without `run` also use `NoRun`. `RunPlace(run: RunIdentity, since: float, first_owner: Owner)` persists through the generic codec in `state.json`. `read_state` defaults missing `run_places` without changing existing state.
- Snapshot copies use `order_since=min(run.since, step.since)`; `waiter_order` sorts `(priority_rank, order_since, pid)`. Durable rows retain step `since` for reconnects, token reacquisition, display age, and AWS eligibility. Process watches remain between steps. The service restores run places and opens watches after restart. Each pass drops ended runs; waiting steps then use their own arrival order. Owners, episodes, and reservations remain per step. Runs hold no memory or CPU token between steps.

**Files:**
- `scripts/build_queue/admission_protocol.py`, `scripts/build_queue/admission_client.py`, `scripts/build_queue/admission_service.py` — run types, parsing, persistence, watches, and snapshot order.
- `scripts/direct/verify.sh`, `scripts/direct/step_record.py` — exported identity and the shared process-start reader; `scripts/lint/memory_admit.py` — snapshot ordering and durable-row filtering.
- `scripts/build_queue/admission_test_support.py`, `scripts/build_queue/test_admission_service.py`, `scripts/build_queue/test_admission_client.py` — shared fixture and run-lifetime regressions.
- `scripts/direct/test_verify_run_place.py`, `scripts/direct/test_step_record.py` — nested, detached, Darwin, and start-command regressions; `docs/as-built/build-memory-admission.md` — run-place contract.
- `scripts/direct/verify_examples_test_support.py` — loop cleanup precedes temporary-folder removal.

**Gotchas:** The PID and start-time string identify a run together. Without `--state-dir`, the service namespace defaults to the buildlog admission folder, including under systemd.
**Ruled out:** Merging `RunIdentity` with `Owner`; removing the client's Darwin guard; storing resolved run order in durable waiter rows.

### Phase 7 — A promote never refuses a step that already waits for memory  · status: done

#### As-built

- `buildlog_wait_for_memory` in `scripts/lint/memory_gate.sh` holds no closed list of `source` values. An unknown source prints with the fallback description of `buildlog_memory_line_inner`; it never refuses.
- After the first wait line (`announced != 0`), a failed checker call prints `memory admission check failed (<last line of the checker output, or "checker returned an error">); trying again, the step keeps its place` to stderr, sleeps the poll interval and polls again. The step is refused with status 2 only when failed calls run for `BUILDLOG_MEM_CHECK_RETRY_S` seconds (default 60) with no success. A successful call clears the failure run. Before the first wait line, a failed call refuses at once.
- `buildlog_memory_check_launch_limit` is the one launch wait limit check (an app launch with no argv; `BUILDLOG_MEM_WAIT_LIMIT_S`, default 900). It runs on the normal path and on the retry path, so failed polls do not extend a launch's limit.
- The caller pid stays the same across polls, so the step's `.waiting` row, its `since` and its `priority_since` stay. All other refusals (invalid output, invalid answer, invalid reading, no reservation, unreadable memory) do not change.

**Files:**
- `scripts/lint/memory_gate.sh` — no source check; the retry while a step waits; `buildlog_memory_check_launch_limit`
- `scripts/lint/test_memory_gate_wait.py` — six regressions that run the real loop: unknown source and one failed poll keep the real waiting row; continued failures refuse with status 2; failed polls do not extend a launch limit; a success resets the retry deadline; an empty checker error uses the fallback text; a failure before the first wait line refuses at once
- `scripts/lint/memory_test_support.py` — `gate_isolation_environment(root)`, which keeps a gate subprocess away from the caller's holds and session records
- `scripts/lint/test_memory_admit.py` — `test_shell_accepts_shape_source` keeps only the `shape` case
- `docs/as-built/build-memory-admission.md` — the fail-closed paragraph states the retry and that the shell does not check `source`
- `docs/plans/builds-production.md` — queue-unit's Units row owns `scripts/lint/memory_gate.sh`

**Binds later work:** Phase 10 ("Every build, view and command uses the queue service…") replaces this wait loop with `admission_client.py`. These wait rules move with the loop, and `test_memory_gate_wait.py` moves to the client with its assertions.

**Gotchas:** a running bash keeps the `memory_gate.sh` functions it read at its start, so a step that waits across a promote runs the old loop against the new checker. For this reason no shell holds a closed list of checker values.

**Ruled out:** a version-matching rule between the shell and the checker; a test that loads a previous release.
