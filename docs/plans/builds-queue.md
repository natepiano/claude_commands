# builds-queue

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The user approved the queue service design on 2026-10-30 (`93c86342`); Phases 2–4 come from it. `/build:queue` answers in under a second with one line per step; then one queue service process holds the queue, each build asks it over a socket and waits, and the polling admission goes.

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

- **Project:** `~/.claude` — the build queue: memory admission, the `/build:queue` and build-why views, and the unit-board queue pane.
- **Project started:** 2026-10-30T11:26:00-04:00
- **Stack:** Python 3.13 (basedpyright strict, unittest); TypeScript mod under `mods/unit-board/` (its own tests in `mods/unit-board/tests/`).
- **Layout:** `scripts/build_queue/` (`build_queue.py`, `build_why.py`, `head_facts.py` from report-unit 4e); `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py`; `scripts/message/roster.py`; `mods/unit-board/hooks/` and `mods/unit-board/tests/`; `commands/build/queue.md`.
- **Key files:** `scripts/lint/memory_admit.py` — admission decisions, the queue rows and `explain_queue`; `scripts/build_queue/build_queue.py` — the queue view and the bump CLI; `scripts/build_queue/build_why.py` — why the head waits; `scripts/build_queue/head_facts.py` — bump rows, head time, token waits; `mods/unit-board/hooks/register.tsx` — `tablePane()` and the `build:queue` command; `mods/unit-board/hooks/queue.tsx` — the pane view; `scripts/build_queue/admission_protocol.py`, `scripts/build_queue/admission_service.py`, `scripts/build_queue/admission_client.py` — the queue service, its socket protocol and its client (new in Phase 3).
- **Test lanes:** `scripts/build_queue/test_*.py`, `scripts/lint/test_memory_admit.py`, `mods/unit-board/tests/queue.test.ts`.
- **Test:** `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY python3 -m unittest <module>` from the worktree root; the mod's test command as `docs/as-built/unit-board.md` names it.
- **Lint:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python <the slot's own Python files>`; footer must read `0 errors, 0 warnings, 0 notes`.
- **Ownership (showrunner, 2026-10-30):** queue-unit owns `scripts/build_queue/` and `commands/build/queue.md` (from report-unit), and `scripts/lint/memory_admit.py` with `scripts/lint/test_memory_admit.py` (from resources-unit). resources-unit keeps its in-flight `memory_admit.py` phases until they land (admit-head, queue order, `/showrunner:bump` priority, strict-window cap, nextest charge drop, 24B); until then each `memory_admit.py` edit is coordinated with resources-unit through berth, and resources-unit reviews the diff. `scripts/message/roster.py` is shutdown-unit's (natedev): moving `unit_labels_for` and its file-only helpers to the new `scripts/message/unit_labels.py` is approved (showrunner, 2026-10-30), and the notice carries an also-touches line. `mods/unit-board/` is natedev unit-board-unit's; the build queue parts of `register.tsx` change under an also-touches line.
- **Invariants:** admission rules live once, in `memory_admit.py`; a view prints admission's answer and repeats no budget or free arithmetic. Only the user bumps, through `/showrunner:bump`. Every answer about the queue comes in under 1 s.

## Queue service design — the user reviews it before any code

- **Who writes it:** a queue-unit design agent, started 2026-10-30 11:3x EDT, read-only; it writes `docs/plans/builds-queue-service-design.md`.
- **What it settles:** today's measured cost of polling admission; the service (what it holds, persists, how it starts and restarts); the socket protocol; admission rules kept in one place; the commands (view, the user's override, `/showrunner:bump` priority, width, release, build log rows); the rule when the service is down; performance targets; tests; and a phased migration from today's polling admission, ordered around resources-unit's in-flight phases and Phase 1.
- **Gate:** queue-unit reviews the draft, sends it to the showrunner, and the showrunner puts it before the user. No service code until the user approves.
- **Approved:** the user, 2026-10-30, at `93c86342` ("yes i want the build queue", relayed by the builds showrunner).
- **After approval:** the design's migration phases become Phases 2+ of this plan through `/plan:to_phased_plan`, each with a Work Order, and go to the showrunner for approval before they run.

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

### Phase 1c — /build:queue reads a lock-free snapshot and answers in under a second every time  · status: todo
**Depends on:** Phase 1, and resources-unit's ordered-walk checkpoint (resources Phase 30, `7106f93d`: the queue starts steps in queue order, and every bump is refused until the user's `/showrunner:bump` path lands) on `origin/builds`; merge `origin/builds` first.
**Estimate:** about 3 h of worker time.

Phase 1 Part B, split out so Part A and Phase 1b land as one checkpoint (showrunner, 2026-10-30). Phase 1 Part A measured 0.4–1.2 s live at load 100–135 (3 of 12 runs over 1.0 s); the admission lock wait is the rest.

#### Work Order

**Goal:** `/build:queue`, its card and build-why read the queue from a lock-free snapshot, so every run answers in under 1 s; the card shows admission's checks for every step, and one identity matcher selects waiting and running steps.

**Bridge to the service:** the `queue.json` snapshot (Spec 1) is how the view answers in under 1 s while polling admission stays. The service design says whether the service's view command replaces it; the view, card and renderer read whichever source the design picks.

**Spec:**

1. **Queue snapshot (one lock-free read).** `memory_admit.py` edits start only after resources-unit's ordered-walk checkpoint is on origin/builds; resources-unit reviews the memory_admit.py diff.
   - Split `read_queue_pass` into `gather_queue_inputs(directory, processes, now, memory: Mapping[Path, StepMemory] = {}) -> QueueInputs` and a pure `queue_read_from(inputs) -> QueueRead`. `gather_queue_inputs` does every file, `/proc` and environment read that `read_queue_pass` does today: waiter rows with their file names, reservation rows with their measured memory and resolved identity, sccache memory, the meminfo reading or unavailable, the raw pressure reading, the raw recovery state, the resolved admission limits, and `now`. `queue_read_from` reads no file, no `/proc` and no environment. `read_queue_pass` calls both, so admission, the locked read and the snapshot reader run one decision path. `memory` lets a caller pass readings it already took; a missing path is read.
   - Recovery: `QueueInputs.recovery: RecoverySpacing` is the raw state that `read_recovery` gives, not the pressure after recovery. `queue_read_from` calls `pressure_with_recovery` itself and passes `recovery` to `admission_walk`. So when a recovery start is due (pressure clear, but only one fitting row may admit), the snapshot gives the same decisions as the locked read.
   - Limits: `QueueInputs.limits: AdmissionLimits` holds the budget and reserve, resolved once in `gather_queue_inputs`. Today `decide` resolves the `ReadHostAdmissionLimits` sentinel from `BUILDLOG_BUILD_BUDGET_GIB` at each call. `queue_read_from` passes `limits` to each call that takes it and never passes `READ_HOST_ADMISSION_LIMITS`, so the reader's own environment never changes the answer.
   - `write_queue_snapshot(directory, inputs)` writes `admission/queue.json` (`{"v": 1, "written_at": <epoch>, "inputs": ...}`) through `queue.json.tmp` and `os.replace`, no fsync (the file is a cache; an unreadable file reads as absent). The serial form and its parser live in `memory_admit.py` only; writer and reader import the same code.
   - One helper calls it at the end of every `LOCK_EX` section: `check`, `release`, `set_width`, and any exclusive section that a later resources checkpoint adds (Phase 26's `give-back`). Since resources Phase 30, `bump_waiter` takes no lock and writes nothing, so it does not call the helper. The write runs after the section's own changes, on success and on an exception path. `check` passes the process table and the memory readings it already took. A failed write (OSError, ValueError) never changes the section's result or exit code; the old file stays and goes stale.
   - `queue_read() -> QueueRead`: parse `queue.json`; when `0 <= time.time() - written_at <= SNAPSHOT_MAX_AGE_SECONDS` (10) and the version and shape are valid, return `queue_read_from(inputs)`; else return `locked_queue_read()`. With waiters, their polls rewrite the file every few seconds; with no waiters nobody polls, so the lock is free. The reading's `now` is the snapshot's `now`; the view shows it as the "as of" time.
   - Moving the `/proc` scan out of the lock and fewer `check` passes stay with resources-unit (not in this phase).
2. **Per-step checks from admission.** `read_queue_pass` already gives each waiter its decision from the one `admission_walk` (resources Phase 30). Keep its numbers: `LineWaiter` gains `decision: AdmissionDecision`, `budget: BudgetCheck` and `free: FreeCheck`, built by the one function that builds the head's `BudgetCheck` and `FreeCheck` today (no second construction). `fits_now` stays.
3. **One selector matcher, from the card's selection.** `memory_admit.match_waiters(selector: str, view: QueueView) -> list[int]` holds the rule that `queue_card.select_waiters` uses today after its shown-name step: a decimal selector is a place (1 to the waiting count); any other selector gives every waiting step whose identity matches (Spec 4's matcher). `queue_card.select_waiters` keeps the shown-name match first (`queue_view.shown_waiters`), then calls `match_waiters` on the snapshot reading. Resources Phase 30 deleted `build_queue.bump_selector` and the match in `bump_waiter`, so the matcher builds on neither. `build_queue.py bump <selector>` keeps Phase 30's refusal: `bump_waiter` raises `BumpOff` before it reads or writes anything, and the CLI prints the fixed text on stdout and exits 3. A bump writes no snapshot.
4. **One identity matcher, the read and the checks.**
   - `memory_admit.step_matches(selector: str, step: WaitingStep | RunningStep) -> bool` is the one identity rule: session id, recorded session name, folder, or folder name. It moves from `queue_card.matches_step`, which goes. `match_waiters` calls it for waiting steps. `queue_card.render_selection` calls it for running steps, after their shown-name match, as `matches_step` does today.
   - `build_queue.py` and `build_why.py` change from `locked_queue_read()` to `queue_read()`.
   - `queue_view.step_checks` returns the head's `HeadExplanation` for #1, `ReadingUnavailable` when the reading or the step's reason is unavailable, and else that step's own `LineWaiter` checks (Spec 2). `ChecksNotRecorded` goes. `queue_view.check_lines` takes the new union and prints the pressure, budget and free lines for any step from one code path. So the card of a step behind #1 prints its budget and free checks.
   - `queue_view.reason_phrase` and `queue_card.order_check` each get an explicit `BehindOlderFit` case. The view's rest line says `behind #<k>, an older step that fits`; the card says `✗ queue order    behind #<k>, an older step that fits`. `#<k>` is the place of the waiting step with the reason's `older_session_id` and `older_since`; when no shown step matches, the text says `an older step` with no place.
5. **One cell width.** One cell width for every Python caller (showrunner, 2026-10-30): `terminal_width` moves to the new `scripts/lib/terminal_cells.py` (a combining mark counts 0, a wide character 2), and `queue_lines.py`, `build_why.py` and `scripts/production/dailies_render.py` import it; `queue_lines.py` and `build_why.py` keep no width function and `dailies_render.display_width` is deleted. dailies_render.py is outside queue-unit's Owns: the showrunner approved the edit; claim it on berth (a holder goes to the showrunner), run `scripts.production.test_dailies_render`, and the notice carries an also-touches line trial-merged against its owner's tip, the switch to `queue_read()`, the docs for those files and the live timing.
6. **The pane's selection is a named state.** `queue.d.ts`: `QueueSelection = { kind: 'view' } | { kind: 'card'; selector: string }`. Each `QueuePaneState` variant holds `selection: QueueSelection` in place of `selector: string | null`. `register.tsx` (build queue parts only) converts at the one load boundary: an older saved state with a string `selector` loads as `card`; one with `null` or no `selector` loads as `view`. No other code reads `selector`. `queueRun` passes the selector only for `card`. `register.tsx`'s `QueueOpening` (`saved | view | card`) uses `QueueSelection` for its `view` and `card` cases, so there is one selection type. The width refresh stays as the code has it: the module-level `queueColumns` and `QueueWidthRefresh` in `register.tsx`, not pane state.
7. **Docs.** `docs/as-built/build-memory-admission.md` (`queue.json` with its recovery and limits fields, `queue_read`, `LineWaiter` checks, `match_waiters`, `step_matches`), `docs/as-built/build-targets.md` (the `/build:queue` row only), `docs/as-built/unit-board.md` (`QueueSelection` and the load conversion). Also correct two lines report-unit 4f left wrong: the index schema number in `build-memory-admission.md` (23, now 24) and the `rebuild_bin` call in `build-targets.md`.

No-copies rule (user, 2026-10-30): one place for each thing. A second copy of setup, a fixture, a config writer, a parser or a helper is a defect. A copy exists only with the user's own approval. Fix at the cause, never a patch at each site.

**Files:**
- `scripts/lint/memory_admit.py` — `gather_queue_inputs` (with `recovery` and `limits`), `queue_read_from`, `write_queue_snapshot` and its calls in every `LOCK_EX` section, `queue_read`, `SNAPSHOT_MAX_AGE_SECONDS`, `LineWaiter` checks, `match_waiters`, `step_matches` (owner queue-unit since 2026-10-30; resources-unit reviews this diff)
- `scripts/lint/test_memory_admit.py` — snapshot and matcher tests
- `scripts/build_queue/build_why.py` — `queue_read()`, cell width from `scripts/lib/terminal_cells.py`
- `scripts/lib/terminal_cells.py` — new: `terminal_width`, the one cell width
- `scripts/lib/test_terminal_cells.py` — new: its test
- `scripts/production/dailies_render.py` — imports `terminal_width`; `display_width` deleted; outside Owns, showrunner approved 2026-10-30
- `scripts/build_queue/build_queue.py` — `queue_read()`; the bump refusal stays
- `scripts/build_queue/queue_card.py` — `select_waiters` on `match_waiters`, running steps through `step_matches`, `matches_step` goes, follower checks, the `BehindOlderFit` order check
- `scripts/build_queue/queue_view.py` — `step_checks` and `check_lines` on `LineWaiter` checks, `ChecksNotRecorded` goes, the `BehindOlderFit` phrase
- `scripts/build_queue/queue_lines.py` — imports `terminal_width`
- `scripts/build_queue/queue_fixture.py` — the fixed 10:46 queue as `QueueInputs`, now with `recovery`, `limits` and per-step checks
- `scripts/build_queue/golden/card_4.txt` — the card of #4 with its budget and free checks
- `scripts/build_queue/test_build_queue.py`, `scripts/build_queue/test_build_why.py`, `scripts/build_queue/test_queue_view.py`, `scripts/build_queue/test_queue_card.py`, `scripts/build_queue/test_queue_lines.py` — changed tests
- `mods/unit-board/types/queue.d.ts` — `QueueSelection`, pane state
- `mods/unit-board/hooks/register.tsx` — build queue parts only: the load conversion and `queueRun` (owner natedev unit-board-unit)
- `mods/unit-board/tests/queue.test.ts` — pane tests
- `docs/as-built/build-memory-admission.md`, `docs/as-built/build-targets.md`, `docs/as-built/unit-board.md` — Spec 7

**Workers:** 1 writer + 1 tester — the Spec fixes the snapshot form, the matchers, the checks and the pane state, so the tester writes from it alone.
- `impl` — `memory_admit.py`, `build_queue.py`, `queue_card.py`, `queue_view.py` (`step_checks`, `check_lines`), `queue_lines.py`, `build_why.py`, `terminal_cells.py`, `dailies_render.py`, `queue.d.ts`, `register.tsx`, the three docs; hub: `scripts/lint/memory_admit.py` (resources-unit reviews it)
- `test` — `test_memory_admit.py`, `test_build_queue.py`, `test_build_why.py`, `test_queue_view.py`, `test_queue_card.py`, `test_queue_lines.py`, `test_terminal_cells.py`, `queue_fixture.py`, the golden files and `queue.test.ts`:
  - Snapshot: a written file round-trips to the same `QueueRead` as the locked read; age 10 s uses it, 10.1 s and a future time take the locked read; wrong `v`, bad JSON and a missing file take the locked read; each of `check`, `release` and `set_width` rewrites it; a failed write leaves the result and the old file; a reader never sees a partial file (temp + replace).
  - Snapshot parity at the recovery boundary: with a recovery start due (pressure clear, two rows that fit), the snapshot and the locked read admit the same one row. Parity with another reader budget: the snapshot read with a different `BUILDLOG_BUILD_BUDGET_GIB` in the reader's environment gives the decisions of the writer's limits.
  - `bump`: the CLI prints Phase 30's refusal on stdout and exits 3, and `queue.json` does not change. The snapshot tests that expected a bump to rewrite the file change to this; each change has an `Assertion-Change:` line that names Spec 3.
  - `match_waiters`: a place, a session id, a recorded session name, a folder and a folder name each select as `queue_card.select_waiters` did before; a shown name still matches first.
  - Running card: a running step is selected by its session id, its recorded name and its folder, through `step_matches`.
  - Follower checks: a step behind #1 prints its own budget and free check lines, equal to the head's construction for the same row. The current fourth-card test that rejects these lines changes to expect them (`Assertion-Change:` naming Spec 4); `card_4.txt` changes to match.
  - `BehindOlderFit`: the view's rest line and the card's order check name `#<k>`, and say `an older step` when no shown step matches.
  - The name fallback as the code does it: an unmatched step shows its recorded session name, else its folder's last part.
  - Pane: an older saved state with `selector: '4'` loads as a `card` selection and one with `selector: null` loads as `view`; the card selector survives a timer refresh and a reload; a width change asks one forced refresh (the module-level `QueueWidthRefresh`, not pane state).

**State:** `admission/queue.json` (new; rewritten under the admission lock at the end of every exclusive section; a cache that readers ignore when older than 10 s). The pane's saved state changes form; the load converts the older form.

**Constraints from prior phases:**
- Phase 1 and 1b: `queue_lines` (`Span`, `Line`, `plain`, `json_form`), `queue_view.render_view`, `queue_card.render_card` and `render_selection`, `queue_fixture.py` and the five golden files exist; build-why uses `unit_labels_for`; the pane paints `QueueLines` and saves `QueuePaneState` with `selector: string | null`.
- Resources Phase 30 (`7106f93d`): `admission_walk(available, total, ordered, running, head, pressure, *, recovery, now, limits)` gives every waiting row its decision in one walk; `queue_decision` runs a full walk and reads one row. `WaitReason` has `BehindOlderFit(older_name, older_since, older_session_id)`. `bump_waiter(_selector, _actor) -> NoReturn` raises `BumpOff` first, with no lock and no write; `build_queue.bump_selector`, `refusal_text`, `WaiterRowSelector` and `UnmatchedWaiter` are gone. `read_queue_pass` reads `read_recovery` and applies `pressure_with_recovery`. Read its merged code before writing; where it differs from this line, its code holds.

**Acceptance gate:**
- `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY python3 -m unittest scripts.build_queue.test_build_queue scripts.build_queue.test_build_why scripts.build_queue.test_head_facts scripts.build_queue.test_queue_view scripts.build_queue.test_queue_card scripts.build_queue.test_queue_lines scripts.lint.test_memory_admit scripts.lib.test_terminal_cells scripts.production.test_dailies_render scripts.message.test_unit_labels scripts.build_targets.test_build_targets scripts.buildlog.test_record scripts.buildlog.test_parse scripts.buildlog.test_test_phase_peak` green.
- `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python` over each slot's own changed Python files: `0 errors, 0 warnings, 0 notes`.
- `command claude plugin test mods/unit-board` green; `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/unit-board` clean.
- Live on natedev, steps waiting, from the worktree's scripts: three runs each of `build_queue.py`, `build_queue.py --json --columns 89`, `build_queue.py 4` and `build_why.py`, every run under 1.0 s wall; the summary lists the 12 times and whether each read the snapshot. No screenshot.
- Live: the snapshot write adds at most 10 ms median to an exclusive hold over at least 200 `check` passes; the summary gives the median and the max.
- The notice has `also touches` lines for `memory_admit.py` and `test_memory_admit.py` (resources-unit, against `origin/builds-resources`; resources-unit reviewed the diff), `dailies_render.py` (its owner, against its tip) and `register.tsx` (natedev unit-board-unit, against `origin/natedev`).

### Phase 2 — One decision pass decides for every waiting step, and the check and the queue read both call it  · status: todo
**Depends on:** Phase 1c
**Blocked by:** resources-unit Phases 30, 31, 32 and 26 merged on `origin/builds` (32 is there at `8a71f222`; in its chain 24 Part B comes before 26). This phase lands before resources Phase 15. Tell resources-unit through berth when it starts and when it lands.
**Estimate:** about 7 h of worker time.

#### Work Order

**Goal:** one pure function, `admission_pass`, decides for every waiting step in one walk; `check`, `read_queue_pass` and the release helpers all call it, so a check and the view give the same decision for the same inputs; the view, card and build-why say what resources Phases 31 and 26 now decide. Builds see no change.

**Spec:**

Source: S1 of `docs/plans/builds-queue-service-design.md`, approved by the user at `93c86342` ("yes i want the build queue"). The design's line numbers are at `d27e24a1` and are out of date; find each function by its name in the merged code. Where the merged resources code differs from this Spec, its code holds and this Spec follows it.

1. **One input type.** The pass takes Phase 1c's `QueueInputs` (from `gather_queue_inputs`), which already holds the raw recovery state (`recovery: RecoverySpacing`) and the resolved limits (`limits: AdmissionLimits`). Do not make a second inputs type. Add to `QueueInputs` only what the pass needs and it lacks: the admit-head override record (Phase 32), the queue priority record (Phase 26) and `blockers_for: frozenset[str]` (owner keys `<pid>-<start_time>` whose blocker the caller wants). Phase 1c's `queue.json` form and parser change with it, in the same place.
2. **The pass.** `admission_pass(inputs: QueueInputs, now: float) -> PassResult` in `memory_admit.py`. It is pure: no file read, no `/proc` read, no environment read, no clock read, no write.
   - One walk for each input scenario. For its inputs the pass calls Phase 30's `admission_walk` once and reads every row's decision from that one result. It never calls `queue_decision`: Phase 30's `queue_decision` runs a full walk to read one row, so a call per row is a second walk. A counterfactual (a running step ends, for `blocking_index` and the release helpers) is its own scenario: a changed copy of the inputs and one walk on it.
   - It also calls the functions that stay as they are: `decide`, `build_charge`, `charge_basis`, `growth_headroom`, `head_priority`, `head_window`, `head_wait_reason`, `head_wait_text`.
   - `queue_decision` then has no caller outside its own tests, so it goes. Its tests read the row's entry from `admission_walk` with the same inputs and the same asserted numbers (`test_memory_admit.py`, and `test_build_why.py` :261-262); each changed call has an `Assertion-Change:` line that names this item.
   - `PassResult` holds `rows: tuple[PassRow, ...]` in queue order, `head: HeadPriority | NoHeadPriority`, `totals: AdmissionDecision` (today's `decide(available, total, 0, memory)`), `recovery: RecoverySpacing` (the state after this pass), `head_marks` (the head-state and priority writes that Phase 26 and today's `check` make, as data), and `override: OverrideKept | OverrideEnded` (Phase 32's record: kept, or ended because its step was admitted or left).
   - `PassRow` holds `owner: str`, `decision: AdmissionDecision`, `reason: WaitReason`, `blocker: BlockingIndex | NoSingleBlocker` (computed only for owners in `blockers_for`), `admit_rank: int | NotAdmitted` (the order in which this pass admits).
3. **Rules the pass states once.** Today the 5 s poll gives these by accident; one pass must state them.
   - Every row the walk admits adds its need to the rows after it (Phase 30's marked needs).
   - In a recovery window (`RecoveryStarts`), the pass admits at most one row. The rows after it hold with the recovery pressure, and `PassResult.recovery` has `next_start_at = now + RECOVERY_START_SECONDS`.
   - Recovery changes move from `check` into one pure function `next_recovery(recovery, pressure, now) -> RecoverySpacing` (a pressure hold gives `PressureHoldRecorded`; a recorded hold that ended gives `RecoveryStarts(now + RECOVERY_WINDOW_SECONDS, now)`; an ended window gives `NoRecoverySpacing`). `check` and `recovery_pressure` call it, and the caller writes the result.
   - A row whose `estimated_at` is `HEAD_SECONDS` or more before `now` holds with a new reason, `EstimateExpired(estimated_at)`. It keeps its place and its room in the walk, so a younger row cannot take that room. Add it to `WaitReason`; every `match` on `WaitReason` gets an explicit `case`. `check` keeps its stderr line `memory admission: estimate expired while waiting for the lock; refresh on the next poll` when its own row gets this reason.
4. **Callers.**
   - `check` keeps its estimate outside the lock, the lock and its file writes. Under the lock it builds `QueueInputs` with `gather_queue_inputs` from the process table and memory readings it already took, adds its own waiter row, calls `admission_pass`, and reads its own `PassRow`. It then writes the head marks, the recovery state, the override end, and its waiter or reservation row, as today. `CheckResult` keeps every field; `head_need`, `head_label`, `head_reason` and `blocker` come from the pass. Its direct calls of `head_priority`, `queue_decision`, `reservation_blocker` and `head_wait_reason` go.
   - `queue_read_from` (Phase 1c), so also `read_queue_pass`, calls `admission_pass` once and builds `QueueView` and `QueueExplanation` from that one `PassResult`: each `WaitingStep.reason` is its `PassRow.reason`, `HeadExplanation` comes from the head's row, and each `LineWaiter` check (Phase 1c Spec 2) comes from its row's decision. It makes no direct call of `head_priority` and no second walk.
   - `reservation_blocker` splits in two. Its rule part, `blocking_index(...) -> BlockingIndex | NoSingleBlocker` (which running step's end would admit the row), is called only by `admission_pass`; each running step it tries is one scenario with one walk. Its identity part, `blocker_identity(index, reservations, processes, proc) -> Blocker` (`QueueIdentityReader`, the sidecar unit), is called by `check`, `blocker_snapshot` and `current_blocker`.
   - The frozen context travels with the explanation. `QueueExplanation` gains `decided_from: QueueInputs`, the inputs its pass ran on. `head_after_steps_end` and `smallest_head_release` stay read-only (Phase 25, Phase 30) and keep their `QueueExplanation` argument: they build the changed copy from `explanation.decided_from` (the ended steps taken out, with its recovery, override, priority and every waiter row) and call `admission_pass` on it. Today they rebuild waiter rows from the explanation and pass `NoRecoverySpacing()`; that goes. Phase 3's `explain` reply carries `decided_from` through the one `QueueInputs` serial form, so build-why gives the same release line from the service.
5. **What the view says for the new rules.** The renderers follow the merged resources code; none keeps its own copy of a rule.
   - Window text: resources Phase 31 Spec 4 adds one function in `memory_admit.py` that writes the window clause from the window, the budget and the head's need (strict with room: `younger steps may use <budget − head need> GiB of the budget until HH:MM`). `head_facts.order_text` calls it and keeps no window text of its own, so the view's strict line, the card's `#1's turn` check and build-why use admission's words.
   - Removed check fields: Phase 31 removes `BudgetCheck.head_need` and `FreeCheck.head_need`. `queue_view.check_lines` drops the `#1` part of the free memory line.
   - Priority and head state: Phase 26 deletes `bump_order` and `holds_younger`. `queue_view.has_move_priority` and the moved-up row filter in `head_facts` (the `bump_order` test) read Phase 26's queue priority record and the waiter's `head_state` in its place. A row with no priority shows no `↑`.
   - A strict-window follower that can start (Rule A: it fits now and fits the head's room) has reason `ReadyForAdmission`: the rest line says `starts at the next check`, its card's order check is `✓ queue order`, and no line says younger steps wait.
6. **`EstimateExpired` output.** It has explicit output in each place, because its resource checks can all pass:
   - `queue_view.reason_phrase`: `estimate refresh`.
   - `queue_view.head_state` reads the head's reason first: an `EstimateExpired` head gives `#1 waits on estimate refresh`, also when its pressure, budget and free checks all pass.
   - The card of an expired step adds the check line `✗ estimate       expired at HH:MM; refreshes on its next poll` above its resource checks.
   - `build_why`: the head's reason text is `estimate refresh: the estimate expired at HH:MM; it refreshes on the next poll`.
7. **No change for builds.** Every existing test passes with no assertion change, except: the `queue_decision` call sites of Spec 2; and the renderer assertions and golden files where resources Phases 31 and 26 changed the text (the window clause, the free memory `#1` part, the priority marks). Each changed assertion has an `Assertion-Change:` line that names the Spec item. The only new text of this phase is the `EstimateExpired` output.
8. **Docs.** `docs/as-built/build-memory-admission.md`: one paragraph, "One decision pass": the inputs, the result, one walk per scenario, the three rules in Spec 3, `decided_from`, and that the check, the view and the release helpers call the same pass.

No-copies rule (user, 2026-10-30): one place for each thing. A second copy of setup, a fixture, a config writer, a parser or a helper is a defect. A copy exists only with the user's own approval. Fix at the cause, never a patch at each site.

**Files:**
- `scripts/lint/memory_admit.py` — `admission_pass`, `PassResult`, `PassRow`, `next_recovery`, `EstimateExpired`, `blocking_index`, `blocker_identity`, the `QueueInputs` fields, `QueueExplanation.decided_from`; `queue_decision` goes; `check`, `queue_read_from`, `head_after_steps_end`, `smallest_head_release` call the pass (owner queue-unit since 2026-10-30; resources-unit reviews the diff)
- `scripts/lint/test_memory_admit.py` — the pass tests
- `scripts/build_queue/queue_view.py` — `reason_phrase` and `head_state` for `EstimateExpired`; `check_lines` without `head_need`; `has_move_priority` on Phase 26's priority
- `scripts/build_queue/queue_card.py` — the estimate check line; the order check on the new window text
- `scripts/build_queue/head_facts.py` — `order_text` calls admission's window clause; the moved-up filter on Phase 26's priority
- `scripts/build_queue/build_why.py` — the `EstimateExpired` reason text
- `scripts/build_queue/queue_fixture.py`, `scripts/build_queue/golden/{view_80,view_100,view_140,card_1,card_4}.txt` — the fixture and renders after Phases 31 and 26
- `scripts/build_queue/test_queue_view.py`, `scripts/build_queue/test_queue_card.py`, `scripts/build_queue/test_head_facts.py`, `scripts/build_queue/test_build_why.py` — the new reason and the new rules
- `docs/as-built/build-memory-admission.md` — Spec 8

**Workers:** 1 writer + 1 tester — the Spec fixes the pass's inputs, result, rules and every new text, so the tester writes from it alone.
- `impl` — `memory_admit.py`, `queue_view.py`, `queue_card.py`, `head_facts.py`, `build_why.py`, `build-memory-admission.md`; hub: `scripts/lint/memory_admit.py` (resources-unit reviews it)
- `test` — `test_memory_admit.py`, `test_queue_view.py`, `test_queue_card.py`, `test_head_facts.py`, `test_build_why.py`, `queue_fixture.py`, the golden files:
  - One `QueueInputs` fed to the check path and to the view path gives the same decision, reason and blocker for every row, in each of: strict window, backfill window, no head priority, pressure, recovery spacing, `BehindOlderFit`, queue priority, admit-head override, reading unavailable, estimate expired.
  - One walk per scenario: a counting wrapper on `admission_walk` sees one call for one `queue_read_from`, and one more call for each counterfactual scenario; `queue_decision` is gone.
  - The pass reads nothing: it runs with the live-memory tripwire armed and `BUILDLOG_PROC` set to an empty folder.
  - Two rows that fit in one pass both admit, and the second is charged with the first's need; in a recovery window only the first admits, and `next_start_at` is set.
  - The release helpers: with a recovery start due in `decided_from`, `head_after_steps_end` gives the same answer as a pass on the inputs with those steps taken out.
  - An expired estimate holds with `EstimateExpired`, and a younger row that fits behind it holds with `BehindOlderFit`.
  - `EstimateExpired` output: an expired head with every resource check passing gives `#1 waits on estimate refresh`; a follower's card shows the estimate check line; build-why prints its reason text; the view's rest line says `estimate refresh`.
  - A strict-window follower that can start: the rest line says `starts at the next check`, its card shows `✓ queue order`, and the strict line is admission's window clause, with no `younger steps wait`.
  - The window clause in the view, the card and build-why is the text of Phase 31's one function for backfill, strict with room and strict with no room.

**Constraints from prior phases:**
- Phase 1c: `gather_queue_inputs` (with raw `recovery` and resolved `limits`), `queue_read_from`, `queue.json` and its one parser, `queue_read`, `LineWaiter` checks (`decision`, `budget`, `free`), `match_waiters` and `step_matches` are in `memory_admit.py`. `queue_view.step_checks` gives #1 its `HeadExplanation` and each other step its `LineWaiter` checks. `queue_view.reason_phrase` has one explicit `case` per `WaitReason` variant, `BehindOlderFit` included.
- Resources Phase 30: one admission walk in queue order (`admission_walk`), `BehindOlderFit`, the bump stop. Phase 31: Rule A is one more check in that walk; `HeadRoom | NoHeadRoom` replaces `AdmissionDecision.head_need`; `BudgetCheck.head_need` and `FreeCheck.head_need` are gone; one window clause function in `memory_admit.py`. Phase 32: `admit_head`, `end_head_override`, `require_showrunner`. Phase 26: queue priority by unit (`queue_priority.json`, `priority set|clear`), `head_state`/`head_since`, the give-back handoff and `queue_times.jsonl`; `bump_order` and `holds_younger` are gone. Phase 24 Part B: `LifetimePromise`, `finalize_reservation`, `reservation_lifetime`. Phase 25: `HeadRelease`, `build_why.release_text` is the one writer of the release line.
- Resources Phase 15 (after this phase) changes `reservation_memory` and the charge, not the pass; it lands on this pass.

**Acceptance gate:**
- `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY python3 -m unittest scripts.lint.test_memory_admit scripts.build_queue.test_build_queue scripts.build_queue.test_build_why scripts.build_queue.test_head_facts scripts.build_queue.test_queue_view scripts.build_queue.test_queue_card scripts.production.test_merge_gate_suites_admission scripts.buildlog.test_record` green; each changed assertion is one Spec 7 allows and has its `Assertion-Change:` line.
- `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python` over each slot's changed Python files: `0 errors, 0 warnings, 0 notes`.
- `grep -rn "queue_decision(" scripts --include=*.py` finds nothing.
- The notice has `also touches` lines for `memory_admit.py` and `test_memory_admit.py` (resources-unit reviewed the diff; trial-merged against `origin/builds-resources`). `build_why.py`, `queue_view.py`, `queue_card.py` and `head_facts.py` are in queue-unit's Owns (`scripts/build_queue/`, a16bda0b), so they need no line.
- Change-Review line: the check and the queue view decide with one pass | on natedev while steps wait, type `/build:queue` and read one waiting step's own wait line in its output | the reason the view gives each step is the reason its own step prints.

### Phase 3 — The queue service runs beside today's admission and logs what it would admit  · status: todo
**Depends on:** Phase 2
**Blocked by:** the two system units of Spec 9 are in in `/etc/nixos/modules/linux/development.nix` on natedev. Queue-unit sent Spec 9 and the `OOMScoreAdjust` question to the natedev showrunner on 2026-10-30 (builds showrunner ask). The service unit has `ConditionPathExists=`, so the units can land before this phase's code is on main.
**Estimate:** about 13 h of worker time.

#### Work Order

**Goal:** one service process, `admission_service.py`, holds the queue in memory and answers a socket protocol that a client and its tests use. Live, it runs as a shadow: it reads today's queue files and writes none of them, runs `admission_pass` every second with its own readings, and logs what it would admit. Builds see no change.

**Spec:**

Source: S2 of `docs/plans/builds-queue-service-design.md` (with §2 The service, §4 Commands, §5 When the service is down and §8 Tests), approved by the user at `93c86342` ("yes i want the build queue"). The design's line numbers are at `d27e24a1`; find each function by its name. The design's `bump {selector, actor}` request and its `bump_order` and `holds_younger` fields are out of date: resources Phase 26 deleted them. This Spec uses Phase 26's queue priority and give-back handoff, and Phase 32's `admit_head`. Where the merged resources code differs from this Spec, its code holds and this Spec follows it.

1. **Three new files in `scripts/build_queue/`.**
   - `admission_protocol.py`: the request and reply types, one encoder and one parser for each message, the version check, and `admission_socket_path() -> Path` (`ADMISSION_SOCKET` when set, else `/run/build-admission/build-admission.sock`). It also holds the one serial form of `QueueRead` and `QueueExplanation` for the `view`, `explain` and `mine` replies, as one writer and one parser. `QueueExplanation.decided_from` (Phase 2) goes through the one `QueueInputs` serial form in `memory_admit.py` (Phase 1c's `queue.json` form); the protocol makes no second form. So build-why's release line from an `explain` reply equals the one from a local pass. It holds `AdmissionUnavailable(reason)`, the one error for a query that has no answer in its bound (Spec 8). The service and the client import it; neither encodes or parses a message itself.
   - `admission_service.py`: `AdmissionServer`, its `Clock` protocol, the shadow source (Spec 7), and the `serve` and `report` commands.
   - `admission_client.py`: the client's Python API and its CLI (Spec 8).
2. **Shared rules move into one function each in `memory_admit.py` (no copy).**
   - Stem rule: `reservation_stem(directory: Path, pid: int, start_time: int, admitted_ns: int) -> Path` gives `admission/<pid>-<start>-<ns>`. `check` names its `.reservation` file from it, and `finalize_reservation` builds its `.max` and `.wave` paths from it. The service's reservation id is the stem's name, and the service sends the `.max` and `.wave` paths in `admitted`. `invoke.sh` :288 still builds `${BUILDLOG_MEM_RESERVATION%.reservation}.max` until Phase 4 replaces it with the path from `admitted`.
   - Estimate: the estimate part at the top of `check` (from `estimate_time` to the profile its locked part uses: `gate_estimate` for a `GateProfile`, the expiry of a cached `StepProfile`, and `git_identity`, `expected_peak` and `filtered_peak` for `InferFromArgv`) moves to `step_estimate(argv: list[str], cwd: str, profile: InferFromArgv | StepProfile | GateProfile, now: datetime, root: Path) -> StepProfile | GateProfile`. `check` calls it, and the client calls it before it connects. Behavior does not change.
   - Inputs: the service builds `QueueInputs` with the same code as Phase 1c's `gather_queue_inputs`. If `gather_queue_inputs` reads files or `/proc` itself, split it: the reads stay in it, and one pure builder takes what it read. It and the service both call that builder.
3. **What the service holds.** One single-threaded loop on one `select.epoll`, with no threads and no locks. In memory:
   - waiters: the connection, the owner (pid, start time), the episode id (Spec 5), the profile and need, `estimated_at`, `since`, Phase 26's `head_state` and `head_since`, the give-back handoff when the `admit` carries one, and the identity (session id, name, step, folder);
   - reservations: the owner, the episode id that admitted it, the scope cgroup path, the need, `admitted_at`, the width, the call id, the shape (resources Phase 24), the sidecar, and the head state copied at admission (Phase 26);
   - the queue priority record (Phase 26) and the admit-head record (Phase 32);
   - the recovery spacing and the last host reading (meminfo, PSI, sccache anon);
   - the last `PassResult`. `view`, `explain` and `mine` read it;
   - the replay ledger (Spec 5): for each admitted episode its `admitted` reply, and for each mutation request id its reply.

   Persisted: one file, `<state dir>/state.json`, written through a temporary file and `os.replace` after each change to the reservations, the priority, the admit-head record, the recovery spacing or the replay ledger. Waiters persist only their order fields (`since`, `head_state`, `head_since`) and their episode id, keyed by owner `<pid>-<start_time>`. The ledger keeps an entry while its reservation lives and for 24 h after it ends. The form of `anon_peaks.jsonl`, `queue_times.jsonl` and the build log rows does not change. The shadow's state dir is `admission/shadow/` (Spec 7). Phase 4's live service uses `admission/` and imports today's files once when `admission/state.json` is absent; because the shadow keeps its state in its own folder, that import still runs.
4. **Start, restart and owners.**
   - At start the service reads `state.json`. For each reservation it checks the owner (pid and start time) and the scope (`cgroup.procs`) with `reservation_lifetime` (`PopulatedStepScope | LiveOwnerProcess | EndedStep`). Live rows keep their charge; ended rows go through `finalize_reservation`. Then it sends `READY=1` to `$NOTIFY_SOCKET`.
   - Each loop turn sends `WATCHDOG=1` to `$NOTIFY_SOCKET` when it is set.
   - The service takes its listening socket from systemd (`LISTEN_FDS`). It binds `admission_socket_path()` itself only when `LISTEN_FDS` is absent (tests, a run by hand).
   - A waiter ends when its connection closes or its owner exits. A reservation ends on `release`, or when its owner has exited and its scope is empty; a reservation with no scope ends when its owner exits. The service watches each owner with `os.pidfd_open`, and each scope's `cgroup.events` for `populated 0` (registered on the epoll with `EPOLLPRI`: kernfs wakes pollers when the file changes; read the file again after each wake). Both are kernel events, so no polling.
   - A connection never holds a reservation: a restart closes every connection and keeps every live reservation (design §2).
5. **Protocol.** Newline-delimited JSON on a Unix stream socket. One request per connection. Every request carries `"v": 1`. A request with an unknown `v` gets `refused {"reason": "client version N, service supports 1..M"}`, and the client stops with that line.

   | Request | Reply |
   | --- | --- |
   | `admit` {episode, owner {pid, start_time}, argv, cwd, since, sidecar, identity {session_id, name, step, folder}, call_id, profile, handoff} | `wait` {reason, position, head, text} lines on each change, and at least every 60 s; then one `admitted` {reservation, need, source, max_path, wave_path} or `refused` {reason} |
   | `launch` {episode, owner, identity} (BRP, no argv) | As `admit`. No reservation is made. |
   | `release` {request_id, reservation, status} | `ok` |
   | `width` {request_id, reservation, width} | `ok` |
   | `give_back` {request_id, reservation} | `ok` {handoff}: Phase 26's handoff. The reservation ends through `finalize_reservation`; the head state and `token_left_at` come back for the step's next `admit`. |
   | `view`, `explain`, `mine` {session_id} | One JSON answer from the last pass |
   | `priority` {request_id, action `set` or `clear`, unit, until, why, actor} | `ok` or `refused` |
   | `admit_head` {request_id, why, actor} | `ok` or `refused` |

   - Admission episodes. The client makes one `episode` id (a random UUID) when one `admit` or `launch` call starts, and sends the same id on each reconnect of that call, `EstimateExpired` resends included. A new acquire by the same owner is a new call, so it has a new id: `verify.sh`'s token path (`buildlog_wait_for_memory`: it admits, releases, waits for a cargo token, then waits again with its first `since`) makes two episodes for one shell. The service keys waiters, reservations and the ledger by episode, not by owner.
   - Replay. The service writes `state.json` with the new reservation and its ledger entry before it sends `admitted`. An `admit` whose episode already has a reservation gets that same `admitted` reply again: no second charge, no second `queue_times.jsonl` row and no second queue place. Each mutation (`release`, `width`, `give_back`, `priority`, `admit_head`) carries a `request_id` (a random UUID the client makes once and resends on reconnect); the service writes its change and its reply to the ledger in one `state.json` write before it replies, and a repeated `request_id` gets the recorded reply with no second change. So a repeated `give_back` returns the same handoff.
   - `wait.reason` is the `WaitReason` name. `wait.text` is the phrase `queue_view.reason_phrase` gives it. In this phase the client prints `text` as it comes; Phase 4 moves today's wait line form from `memory_gate.sh` into the client.
   - On a `wait` with reason `EstimateExpired` (Phase 2), the client runs `step_estimate` again, closes, and sends a new `admit` with its first `since`.
   - The service writes the build log rows that the lock holder writes today (`anon_peaks.jsonl` at release, `queue_times.jsonl` through Phase 26's one append function, the priority rows), from the same functions.
   - The socket file is mode 0600, so only the user's processes connect. The service records `actor`. `require_showrunner` stays in the CLI that sends `priority` or `admit_head` (design §4).
6. **When a pass runs.** On every event: an `admit`, a close, a `release`, an owner exit, an empty scope, a `width`, a `give_back`, a `priority` or an `admit_head`. Also once a second while any waiter waits, because memory freed outside builds shows only in `/proc/meminfo`. A pass reads meminfo, PSI, sccache's `memory.stat` and each running scope's `memory.current` and `memory.swap.current` through the readers in `memory_admit.py` (`read_meminfo`, `read_pressure`, `sccache_memory`, `reservation_memory`). It reads the `/proc` tree only for reservations with no scope. It then calls `admission_pass` (Phase 2) and acts on the result: it admits rows in `admit_rank` order, writes the head marks and the recovery spacing, ends the override when the result says so, and sends a `wait` line to each waiter whose reason or position changed.
7. **The shadow, this phase's live run.** `admission_service.py serve` (the unit's `ExecStart`) runs the shadow. Its state dir is `admission/shadow/`; `--state-dir <dir>` changes it (the gate's run).
   - Waiters come from Phase 1c's `queue.json` (its one parser). Reservations come from the `.reservation` rows, parsed by the function today's readers use. The shadow reads both each second with no lock and writes neither. Both are written through `os.replace`, so a read sees a whole file.
   - Episodes in the shadow. Today's files carry no client episode id, so the shadow makes one: an episode opens when an owner's waiter row appears in `queue.json` and that owner has no open episode, and its id is `<pid>-<start_time>-<opened ns>`. It closes when a `.reservation` row for that owner first appears with `admitted_at` at or after the episode opened, or when the owner leaves `queue.json` with no such row. Open episodes persist in the shadow's `state.json`, so a shadow restart keeps them. A release and a new wait by the same owner open a second episode.
   - Each second it runs `admission_pass` on those rows with its own fresh readings and its own recovery spacing. A row it would admit counts in later passes as a reservation at its need until its episode closes.
   - It appends rows to `<state dir>/decisions.jsonl` through the build log's one line appender (`store.append_line`): `{"at", "kind": "would_admit", "episode", "owner", "need", "snapshot_at", "snapshot_reason"}` the first time a pass admits an episode (`snapshot_reason` is the reason `admission_pass` gives that owner on `queue.json`'s own inputs, and `snapshot_at` is its `written_at`); and `{"at", "kind": "admitted", "episode", "owner", "admitted_at"}` when the `.reservation` row that closes an episode appears.
   - It answers `view`, `explain` and `mine` from its own pass. It answers every other request with `refused {"reason": "shadow: today's admission decides"}`.
   - `admission_service.py report --since <ISO time> [--state-dir <dir>]` reads `decisions.jsonl` and prints four numbers and a verdict. Real admissions. Matched: sort the real admissions by `admitted_at` and the same episodes by their `would_admit` time; a real admission matches when its episode has a `would_admit` row before its `admitted` row and has the same index in both lists. Held at the instant: `would_admit` rows whose `snapshot_reason` is `NeedsBuildBudget`, `NeedsMemory` or a pressure hold, with `snapshot_at` within 1 s of `at`. Median lead: `admitted_at` minus the `would_admit` time. The verdict is `adopt` when there are at least 200 real admissions, at least 99 % match, none is held at the instant, and the median lead is at least 2 s (design S2); else `not yet`.
8. **The client, `admission_client.py`.**
   - Python API: `admit(request: AdmitRequest, on_wait: Callable[[Wait], None]) -> Admitted | Refused`, `launch(...)`, `release(reservation: str, status: int)`, `width(reservation: str, width: int)`, `give_back(reservation: str) -> Handoff`, `view() -> QueueRead`, `explain() -> QueueExplanation`, `mine(session_id: str)`, `priority(...)`, `admit_head(why: str, actor: str)`. Phase 4's merge gate calls this API in process.
   - CLI: one subcommand for each request (`admit`, `launch`, `release`, `width`, `give-back`, `view`, `explain`, `mine`, `priority set|clear`, `admit-head`). `admit` prints each `wait.text` to stderr; on `admitted` it prints one protocol line to stdout and exits 0; on `refused` it prints the reason to stderr and exits nonzero.
   - Queries are bounded. `view`, `explain` and `mine` wait for no admission: each connects and reads its one answer within `QUERY_TIMEOUT_SECONDS` (0.5 s) in all, or raises `AdmissionUnavailable` with the reason (`no socket`, `connect refused`, `no answer in 0.5 s`). It does not try again. Their CLI prints `admission service unavailable: <reason>` on stderr and exits 2. Phase 4's `/build:queue`, its card, build-why and the pane keep their own failure output on this error.
   - The waiting rule below is for `admit`, `launch` and the mutations, which each resend with the same episode or `request_id`, so a retry is safe (Spec 5).
   - When the service is down (design §5), the client never falls back to the lock. A closed connection: it connects again at once and sends its first request again, with its first `since` and its episode or `request_id`, and prints `admission service restarted; waiting again` once. No socket, or a refused connect: it tries again every 2 s and prints one line a minute. No `wait` line for 120 s: it prints `admission service not answering since HH:MM` (time in the user zone, through `user_zone.py`) and keeps waiting.
9. **systemd system units (queue-unit writes them in an `/etc/nixos` worktree; natedev reviews, commits and pushes; they go live at the user's next rebuild).** natedev, 2026-10-30: a user unit cannot set `OOMScoreAdjust=-900`, because natedev's user manager runs at `oom_score_adj` 100 and an unprivileged process cannot go below its own floor. So both units are system units that run as the user. Do not lower `user@1000`'s score: that would protect every user process, builds included.
   - `build-admission.socket`: `ListenStream=/run/build-admission/build-admission.sock`, `SocketUser=natepiano`, `SocketMode=0600`, `RuntimeDirectory=build-admission`, `WantedBy=sockets.target`.
   - `build-admission.service`: `User=natepiano`, `Type=notify`, `NotifyAccess=all`, `ExecStart=/home/natepiano/.claude/scripts/lib/py /home/natepiano/.claude/scripts/build_queue/admission_service.py serve`, `ConditionPathExists=/home/natepiano/.claude/scripts/build_queue/admission_service.py`, `Restart=always`, `RestartSec=1`, `WatchdogSec=30`, `OOMScoreAdjust=-900`, `CPUWeight=1000`, outside `builds.slice`, `WantedBy=multi-user.target`. It starts at boot, so shadow mode runs from boot (natedev, /etc/nixos 45bc6bb); the socket also starts it on the first connect.
   - Phase 4 adds `OnFailure=` and an alert unit.
10. **Tests.** `AdmissionServer(state_dir, clock, readings, proc)` runs its loop in the test thread on a socket in the test's temporary folder, and `ADMISSION_SOCKET` points the client at it. One support module, `scripts/build_queue/admission_test_support.py`, makes the server, the fake clock and the per-test socket; every test that needs a server imports it (Phase 4's 13 moved modules too). The `Clock` protocol gives `now()` and timers; tests move it across `RAMP_SECONDS`, `HEAD_SECONDS`, the strict and backfill windows and the recovery spacing with no sleep. Readings come from `fake_memory_readings` as today, with the live-memory tripwire armed, so it covers the service's readers. Owners are short `sleep` children the test kills, so pidfd events are real; the scope case uses a real transient scope (`systemd-run --user --scope`). The modules skip on a host with no pidfd or cgroup v2 (the Mac) and print that reason.
11. **Docs.** `docs/as-built/build-memory-admission.md`: one paragraph, "Queue service (shadow)": the three files, the protocol table, the state file, the shadow log and its report, and that no build uses the service yet.

No-copies rule (user, 2026-10-30): one place for each thing. A second copy of setup, a fixture, a config writer, a parser or a helper is a defect. A copy exists only with the user's own approval. Fix at the cause, never a patch at each site.

**Decided (showrunner, 2026-10-30): how the service runs new code after a merge to `~/.claude` main.** Option A. The service stats each source file it loaded from `~/.claude/scripts` (from `sys.modules`) once a second. When a file's mtime or inode differs from its start, and no file has changed for 2 s (so a checkout that is still writing is not read half-written), the service writes `state.json`, sends `STOPPING=1` and exits 0; systemd (`Restart=always`, `RestartSec=1`) starts the new code. No other session acts, and a restart costs a waiter about 1 s. Phase 4's cut-over uses the same rule.

**Files:**
- `scripts/build_queue/admission_protocol.py` — new: the messages, their encoder and parser, the version check, `admission_socket_path`, the `QueueRead` and `QueueExplanation` serial form
- `scripts/build_queue/admission_service.py` — new: `AdmissionServer`, `Clock`, the shadow source, `serve`, `report`
- `scripts/build_queue/admission_client.py` — new: the Python API and the CLI
- `scripts/build_queue/admission_test_support.py` — new: the one in-process server fixture
- `scripts/build_queue/test_admission_service.py`, `scripts/build_queue/test_admission_client.py` — new tests
- `scripts/lint/memory_admit.py` — `reservation_stem`, `step_estimate`, the pure `QueueInputs` builder; `check` and `finalize_reservation` call them (owner queue-unit since 2026-10-30; resources-unit reviews the diff)
- `scripts/lint/test_memory_admit.py` — tests of the three
- `docs/as-built/build-memory-admission.md` — Spec 11

**Workers:** 1 writer + 1 tester — the Spec fixes the protocol, the server's constructor, the client API and the shadow log form, so the tester writes from it alone.
- `impl` — `admission_protocol.py`, `admission_service.py`, `admission_client.py`, `memory_admit.py`, `build-memory-admission.md`; hub: `scripts/lint/memory_admit.py` (resources-unit reviews it)
- `test` — `admission_test_support.py`, `test_admission_service.py`, `test_admission_client.py`, `test_memory_admit.py`:
  - `admit` gets `wait` lines and then `admitted`; two waiters admit in `admit_rank` order; a `release` frees the charge in the next pass, under 0.2 s of fake time.
  - A crash between `admitted` and the first `release`: a new server on the same state dir keeps the reservation and its charge.
  - A crash after the `state.json` write and before the reply is sent (a fault hook in `admission_test_support.py`), once for `admit` and once for `give_back`: a new server on the same state dir answers the client's resend with the same `admitted` reply or the same handoff, with one charge, one `queue_times.jsonl` row and one handoff.
  - A repeated `request_id` for `release` and `priority` gets the recorded reply and makes no second change.
  - A restart with 30 waiters: each connects again with its `since`, and the order and `head_since` stay.
  - An owner killed while it waits leaves the queue at once; an owner with no scope killed while it holds a reservation releases it; a scope that outlives its shell keeps the charge until `populated 0`.
  - A client of version 2 against a service of version 1 gets `refused` with both versions.
  - A hung service (it accepts and sends no line): after 120 s of fake time the client prints `admission service not answering since HH:MM` and keeps waiting.
  - Bounded queries: with no socket, and with a service that accepts and sends nothing, `view`, `explain` and `mine` each raise `AdmissionUnavailable` within 0.5 s, and the CLI exits 2 with the reason on stderr.
  - No socket: the client tries every 2 s, prints one line a minute, and never returns `Admitted`.
  - `give_back` returns Phase 26's handoff, and the next `admit` with it appends the `token_gap` row.
  - `priority` and `admit_head` change the next pass and record the actor.
  - `EstimateExpired`: the client estimates again and keeps its `since`.
  - The shadow, on fixture `queue.json` and `.reservation` files: it writes no file outside its state dir, logs `would_admit` and `admitted` rows, and refuses change requests; `report` prints the four numbers and the verdict for a fixture log.
  - Repeated episodes for one owner: one owner admits, releases and waits again with its first `since` (the `verify.sh` token path). The shadow logs two episodes, and `report` pairs each `admitted` row with its own `would_admit` row. A shadow restart between the two keeps the open episode.
  - The `explain` reply round-trips `decided_from`, and build-why's release line from it equals the local one.
  - `reservation_stem`, `step_estimate` and the `QueueInputs` builder each have a test, and every existing `check` test passes with no changed assertion.

**Constraints from prior phases:**
- Phase 2: `admission_pass(inputs: QueueInputs, now: float) -> PassResult` in `memory_admit.py` is the one decision for every waiting row; it walks once per input scenario, and `queue_decision` is gone. `QueueExplanation.decided_from: QueueInputs` holds the frozen inputs, and `head_after_steps_end` and `smallest_head_release` run the pass on a changed copy of it. `PassRow` has `owner`, `decision`, `reason`, `blocker`, `admit_rank`; `PassResult` has `head_marks`, `recovery` and `override`. `next_recovery` is the one recovery change. `EstimateExpired` is a `WaitReason`. `blocking_index` and `blocker_identity` replace `reservation_blocker`.
- Phase 1c: `queue.json` (`{"v": 1, "written_at", "inputs"}`) and its one parser, which is also the one `QueueInputs` serial form; `gather_queue_inputs` (with raw `recovery` and resolved `limits`), `queue_read_from`, `match_waiters`, `step_matches`, `queue_view.reason_phrase`.
- Resources Phase 24 Part B: `finalize_reservation`, `reservation_lifetime`. Phase 26: queue priority (`priority set --unit --until --why`, `queue_priority.json`), `head_state` and `head_since`, the give-back handoff, `queue_times.jsonl` and its one append function; `bump_order`, `holds_younger` and `bump_waiter` are gone. Phase 32: `admit_head`, `end_head_override`, `require_showrunner`.

**Acceptance gate:**
- `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY python3 -m unittest scripts.build_queue.test_admission_service scripts.build_queue.test_admission_client scripts.lint.test_memory_admit scripts.build_queue.test_build_queue scripts.build_queue.test_build_why scripts.build_queue.test_queue_view scripts.production.test_merge_gate_suites_admission scripts.buildlog.test_record` green, with no changed assertion in an existing test.
- `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python` over each slot's changed Python files: `0 errors, 0 warnings, 0 notes`.
- `grep -n "json.loads\|json.dumps" scripts/build_queue/admission_service.py scripts/build_queue/admission_client.py` finds nothing: every message goes through `admission_protocol.py`.
- Live on natedev while steps wait, from the worktree: run `~/.claude/scripts/lib/py scripts/build_queue/admission_service.py serve --state-dir <gate folder>` with `ADMISSION_SOCKET` in the gate folder for 30 min. No file under `admission/` outside the gate folder changes because of it; `decisions.jsonl` has `would_admit` and `admitted` rows; `report` prints its numbers; 20 timed `admission_client.py view` calls each finish in under 0.25 s. The summary gives the times and the report.
- After the checkpoint is on main, the builds showrunner starts the service once by connecting (`admission_client.py view`; the socket starts it) and checks `systemctl show -p MainPID,ControlGroup build-admission.service`: the cgroup is not under `builds.slice`, and `/proc/<MainPID>/oom_score_adj` reads `-900`. The 24 h shadow run that Phase 4 waits on starts then.
- The notice has `also touches` lines for `memory_admit.py` and `test_memory_admit.py` (resources-unit reviewed the diff; trial-merged against `origin/builds-resources`).
- Change-Review line: the queue service runs as a shadow and logs what it would admit | on natedev while steps wait, run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/admission_client.py view`, then `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/admission_service.py report --since <one hour ago>` | the view answers in under a quarter second, and the report counts real admissions, matched admissions and the median lead.

### Phase 4 — Every build, view and command uses the queue service, and the polling admission goes  · status: todo
**Depends on:** Phase 3
**Blocked by:** (1) `admission_service.py report` over at least 24 h of main's shadow prints `adopt` (Phase 3 Spec 7); (2) resources-unit Phases 27 and 33 merged on `origin/builds` (33 edits `release` and the owner-gone finalizer; resources-unit, 2026-10-30; 30 and 31 are before this phase through Phase 2), and resources 20 not yet started (its Decided block); (3) the natedev NixOS session has `OnFailure=` and the alert unit of Spec 6 in place (the alert unit only calls a command, so it can land first). Tell resources-unit, verify-unit and the natedev showrunner through berth when this phase starts and when it lands.
**Estimate:** about 23 h of worker time.

#### Work Order

**Goal:** every build step, BRP launch, merge gate, stop, view and command asks the service, all in one merge, and the same merge deletes the polling admission: `check`, the locks, the file readers, the poll loop, `.waiting`, `.reservation`, `admission/lock` and Phase 1c's `queue.json` writer. A step starts only on an `admitted` answer, each step starts Python once instead of once every 5 s, and the view answers in under 0.25 s.

**Spec:**

Source: S3 of `docs/plans/builds-queue-service-design.md` (with §3 Decisions stay in one place, §4 Commands, §5 When the service is down and §8 Tests), approved by the user at `93c86342`. The design's views and commands phase (the former S4) is part of this phase, because a separate phase would keep the `queue.json` writer alive as a second copy of queue state. The design's line numbers are at `d27e24a1`; find each function by its name. Where the merged resources code differs from this Spec, its code holds and this Spec follows it.

1. **One cut-over, no second path (user, 2026-10-30 11:42 EDT).** Every caller moves in this phase's one merge. There is no mode file, no fallback to the lock and no second admitter: callers split between the lock and the service would be two admitters that cannot see each other's reservations, so both admit into the same free memory. The only rollback is a git revert of this phase, which brings back the Phase 3 shadow.
2. **The live service.** `admission_service.py serve` runs the live server with state dir `admission/`. The shadow source, `decisions.jsonl`, the `report` command and `admission/shadow/` go.
   - One import at start, only when `admission/state.json` is absent: each `.reservation` row (through `reservation_lifetime`: live rows keep their charge, ended rows go through `finalize_reservation`), each `.waiting` row's order fields keyed by owner, the queue priority record (`queue_priority.json`), Phase 32's admit-head record and the recovery spacing (`read_recovery`). The service writes `state.json`, then deletes the files it imported. A later start finds `state.json` and imports nothing.
   - Each admission appends one row to `queue_times.jsonl` through Phase 26's one append function: `{"at", "kind": "admitted", "session_id", "step", "pid", "start_time", "call_id", "need", "waited_s"}`. The 24 h check in the gate reads it.
   - The new code starts through Phase 3's restart rule (its Decided block).
   - The drain barrier, `admission_service.py drain-check`. It reads only, and it uses the import's own reader of `.waiting` and `.reservation` rows (no second parser). It counts three things: waiter rows whose owner is alive (pid and start time), reservation rows that `reservation_lifetime` gives as live, and live processes that hold the old gate functions. A process holds them when its command line runs a script that sources `memory_gate.sh` directly or through `invoke.sh` (`verify.sh`, `invoke.sh`, `pre-tool-use-brp-launch-gate.sh`), or runs `merge_gate.py` (it calls `memory_admit.check` in process). The list is one constant in the barrier. With all three counts 0 it prints `drained` and exits 0; else it prints each count with up to 5 owners or pids and exits 1.
3. **Cut-over steps.** The builds showrunner runs them at the merge to main, in a quiet hour. A running bash keeps the `memory_gate.sh` functions it read at its start, so a step that started before the merge would call the deleted `check`.
   1. `/build:hold hold "admission cut-over"` on natedev.
   2. Wait until `/build:hold quiet` reports quiet and the drain barrier reports `drained`: `~/.claude/scripts/lib/py <merge checkout>/scripts/build_queue/admission_service.py drain-check`, run from the merge checkout before the push. `/build:hold quiet` alone is not enough: its `quiet_verdict` counts only processes whose command name is a build command, so a shell that waits in admission (its poll sleeps between `check` passes) or that holds the old gate functions reads as quiet.
   3. Merge to main. The service restarts on the new code, imports, and answers.
   4. `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/admission_client.py view` answers from the live service.
   5. `/build:hold release`.
4. **Callers.**
   - `memory_gate.sh`: `buildlog_wait_for_memory` runs one foreground `admission_client.py admit ...` and reads its `admitted` line. `buildlog_release_memory` runs `admission_client.py release`, `buildlog_give_back_memory` runs `give-back`, and the width call runs `width`. Today's wait line form (`buildlog_memory_line_inner` and the `printf` lines in `buildlog_wait_for_memory`) and the leave-hook loop (`LINT_QUEUE_LEAVE_HOOK`, else `BUILDLOG_MAC_IDLE_HOOK`, run every 5 s; a success closes the connection and exits as `LeftQueue`) move into `admission_client.py`, one function each, and the shell copies go. The client prints the same lines as today from the `wait` messages, and `memory_gate.sh` keeps today's exit codes for admitted, refused and left.
   - `invoke.sh` :288: the `.max` path comes from the `admitted` line (`max_path`), not from `${BUILDLOG_MEM_RESERVATION%.reservation}`. `BUILDLOG_MEM_RESERVATION` holds the reservation id, not a file path. The sampler `buildlog_sample_anon` does not change.
   - `verify.sh`: the cancel path (:1171–1181) drops its reservation list, because the owner's end releases. The give-back call site keeps the name `buildlog_give_back_memory`.
   - `pre-tool-use-brp-launch-gate.sh`: runs `admission_client.py launch`.
   - `build_hold.py` `admitted_processes`: owners from `view`'s reservations. A service that does not answer within the query bound raises `AdmissionUnavailable` (Phase 3), and the callers treat it as they treat an unreadable admission folder today.
   - `override_guard.py` and `override_guard.sh`: `ADMISSION_SOCKET` joins `OVERRIDE_VARIABLES`, so the installed client refuses a socket outside the test's own folder, as it refuses `BUILD_HOLD_DIR` today. `admission_socket_path()` calls the guard; there is no second refusal check.
   - `merge_gate.py` `gate_reservation`: calls `admission_client.admit` and `release` in process (Phase 3's Python API) in place of `memory_admit.check`.
   - `stop.py` `_release_build_claims`: its admission part goes, because the service ends a stopped owner's waiter and reservation from its pidfd. Its `admitted(owner)` check asks `mine`.
   - `mac_idle.py` `waiting_evidence`: waiting steps come from `view`, not from `.waiting` files.
   - `build_why.py` asks `explain`. `build_queue.py` asks `view` and `mine`, and its priority and admit-head commands send `priority` and `admit_head` (`require_showrunner` stays in the CLI). `commands/build/queue.md` names the new source.
   - Queue questions fail within the bound, apart from admission waiting. On `AdmissionUnavailable`, `build_queue.py` (the view and the card) and `build_why.py` print `build queue: admission service unavailable: <reason>` on stderr and exit 2 within 1 s. The pane keeps its one failure message, `Build queue could not be read.`, and keeps its `QueueSelection` (Phase 1c), so the next refresh opens the same card.
   - `memory_gate.sh` makes one episode per `admission_client.py admit` call (Phase 3 Spec 5). So `verify.sh`'s token path, which admits, releases and waits again, makes two episodes; each keeps its first `since`.
5. **Deletions in `memory_admit.py`.** `check`, `locked_queue_read`, `current_blocker`, `blocker_snapshot`, `live_waiters`, `live_reservations`, `queue_rows`, `cancel_waiter`, `release`, `set_width`, `write_admission_record`, the give-back CLI, `run_action`'s file protocol (it keeps only the actions with a caller outside admission, such as `tripwire`), every `flock`, Phase 1c's `write_queue_snapshot`, the `queue.json` file read and the snapshot path of `queue_read`, and the writers of the queue priority file and the admit-head file. The `QueueInputs` serial form stays: the protocol's `explain` reply uses it (Phase 3). The rules, estimates and readers stay (design §3): `admission_pass`, `admission_walk`, `decide`, `build_charge`, `charge_basis`, `growth_headroom`, `head_priority`, `head_window`, `head_wait_text`, `blocking_index`, `blocker_identity`, `head_after_steps_end`, `smallest_head_release`, `expected_peak`, `filtered_peak`, `gate_estimate`, `step_estimate`, `read_meminfo`, `read_pressure`, `reservation_memory`, `sccache_memory`, `queue_read_from`, `reservation_stem`, `finalize_reservation`, `reservation_lifetime`. The service is the only caller of `admission_pass`, directly or through `head_after_steps_end` and `smallest_head_release`.
6. **Down alert.** `OnFailure=` on `build-admission.service` starts `build-admission-alert.service` (`Type=oneshot`, written by the natedev NixOS session), whose `ExecStart` is `%h/.claude/scripts/lib/py %h/.claude/scripts/build_queue/admission_client.py down-alert`. The command waits until the service has been down for 2 min (the socket refuses, or no answer), takes a claim with `outage_alerts.claim("build-admission")`, sends `build admission service is down; builds wait` through `send.py`'s outage path with a new `push_catalog.py` entry, and calls `outage_alerts.delivered`. A claim that is in flight or delivered sends nothing, so there is one push for each outage. The service calls `outage_alerts.clear("build-admission", now)` after each start.
7. **Tests.** These 13 modules move to a per-test `ADMISSION_SOCKET` with the in-process server from `admission_test_support.py` (Phase 3): `test_verify_hold_memory`, `test_verify_token_reservation`, `test_verify_tree_cancel_reacquire`, `test_verify_checks_fail_closed`, `test_verify_lint_guard_key`, `verify_tree_cancel_test_support`, `test_brp_launch_gate`, `test_build_hold`, `build_hold_test_support`, `test_hold_shortcut`, `test_mac_idle`, `test_bounded_stop`, `test_merge_gate_suites_admission`. The merge-gate test runs start an in-process server in place of real check and release processes against fake state under `/dev/shm`. Tests of deleted functions go with them.
8. **Docs.** `docs/as-built/build-memory-admission.md`: rewrite the admission part for the service (what it holds, the protocol, release by pidfd and scope, the rule when it is down, the import, the restart rule) and delete the lock, `.waiting`, `.reservation` and `queue.json` parts. `commands/build/queue.md`: the new source.

No-copies rule (user, 2026-10-30): one place for each thing. A second copy of setup, a fixture, a config writer, a parser or a helper is a defect. A copy exists only with the user's own approval. Fix at the cause, never a patch at each site.

**Decided (showrunner, 2026-10-30): where this phase lands in resources-unit's chain.** Option A: after resources 27 and before 20 (chain 21 → 16 → 25 → 24 → 26 → 15 → 27 → **this phase** → 20 → 11 → 17 → 18 → 19 → 8). 27 is the one remaining resources phase that changes a caller this phase moves (`gate_reservation`), so landing after it moves that caller once; 20, 11, 17, 18 and 19 change rules and estimates that stay in `memory_admit.py`, which the service runs; 8 decides whether the service's `state.json` is shared build state. queue-unit confirms this place with resources-unit against its Work Orders.

**Files:**
- `scripts/lint/memory_admit.py` — Spec 5 deletions (owner queue-unit since 2026-10-30; resources-unit reviews the diff)
- `scripts/lint/test_memory_admit.py` — tests of deleted functions go
- `scripts/build_queue/admission_service.py` — the live server, the import, the `admitted` row, `drain-check`; the shadow and `report` go
- `scripts/build_queue/admission_client.py` — the wait line form, the leave hook, `down-alert`
- `scripts/build_queue/admission_protocol.py` — `admission_socket_path` calls the override guard
- `scripts/build_queue/build_why.py`, `scripts/build_queue/build_queue.py`, `commands/build/queue.md` — `explain`, `view`, `mine`, `priority`, `admit_head`, the `AdmissionUnavailable` output (owner queue-unit)
- `scripts/lint/memory_gate.sh` — the client calls; the wait line form and the leave-hook loop go (owner resources-unit)
- `scripts/hooks/pre-tool-use-brp-launch-gate.sh` — `launch` (owner resources-unit)
- `scripts/build_hold/build_hold.py` — `admitted_processes` (owner resources-unit)
- `scripts/build_hold/override_guard.py`, `scripts/build_hold/override_guard.sh` — `ADMISSION_SOCKET` (owner resources-unit)
- `scripts/lint/invoke.sh` — the `.max` path from `admitted` (owner verify-unit)
- `scripts/direct/verify.sh` — the cancel path (owner verify-unit)
- `scripts/buildlog/mac_idle.py` — `waiting_evidence` (owner verify-unit, per `docs/plans/builds-production.md`)
- `scripts/production/merge_gate.py` — `gate_reservation` (owner the natedev showrunner)
- `scripts/shutdown/stop.py` — `_release_build_claims` (owner the natedev showrunner)
- `scripts/notify/push_catalog.py` — the down push entry (natedev messaging-unit's file; the showrunner gave queue-unit the edit, 2026-10-30; also-touches line for natedev, trial-merged against natedev's tip)
- `scripts/accounts/outage_alerts.py` — called for `claim`, `delivered` and `clear`; changed only if a claim with a key that is not a session id needs it (the showrunner gave queue-unit the edit, 2026-10-30; also-touches line for natedev, trial-merged against natedev's tip)
- `scripts/direct/test_verify_hold_memory.py`, `scripts/direct/test_verify_token_reservation.py`, `scripts/direct/test_verify_tree_cancel_reacquire.py`, `scripts/direct/test_verify_checks_fail_closed.py`, `scripts/direct/test_verify_lint_guard_key.py`, `scripts/direct/verify_tree_cancel_test_support.py`, `scripts/buildlog/test_mac_idle.py` — per-test socket (owner verify-unit)
- `scripts/hooks/test_brp_launch_gate.py`, `scripts/build_hold/test_build_hold.py`, `scripts/build_hold/build_hold_test_support.py`, `scripts/build_hold/test_hold_shortcut.py` — per-test socket (owner resources-unit)
- `scripts/shutdown/test_bounded_stop.py`, `scripts/production/test_merge_gate_suites_admission.py` — per-test socket (owner the natedev showrunner)
- `scripts/build_queue/test_admission_service.py`, `scripts/build_queue/test_admission_client.py`, `scripts/build_queue/test_build_queue.py`, `scripts/build_queue/test_build_why.py` — the live service, the import, the drain barrier, the client's lines, the down alert, the bounded query failures
- `mods/unit-board/tests/queue.test.ts` — the pane keeps its failure message and selection when the service is unavailable (owner natedev unit-board-unit)
- `docs/as-built/build-memory-admission.md` — Spec 8

**Workers:** 1 writer + 1 tester — the cut-over cannot split: callers split between the lock and the service are two admitters (Spec 1), so one writer moves every caller, and the tester writes from the Spec alone.
- `impl` — every code file above, `commands/build/queue.md` and `build-memory-admission.md`; hub: `scripts/lint/memory_admit.py` (resources-unit reviews it)
- `test` — the 13 moved modules, `test_memory_admit.py`, `test_admission_service.py`, `test_admission_client.py`, `test_build_queue.py`, `test_build_why.py`, `queue.test.ts`:
  - The import runs when `state.json` is absent and keeps each live reservation's charge; a second start imports nothing.
  - `memory_gate.sh`'s admit, release, give-back and width against an in-process server print today's lines and exit with today's codes; the leave hook exits as `LeftQueue`.
  - Each admission appends one `admitted` row to `queue_times.jsonl`.
  - The BRP launch gate, `admitted_processes`, `_release_build_claims`, `waiting_evidence` and `gate_reservation` each work against an in-process server.
  - The override guard refuses an `ADMISSION_SOCKET` outside the test's own folder.
  - `down-alert` with a fake send: one push for each outage, none while a claim is in flight or delivered, and the claim clears after a start.
  - The 13 modules pass on a per-test socket and never reach the live service.
  - The drain barrier, on fixture files that hold the state before the merge that deletes `check`: an old `.waiting` row with a live owner and no running build command gives not drained and the count; a live reservation gives not drained; a live process whose command line runs `verify.sh` gives not drained; none of these gives `drained`.
  - Bounded query failures: with no socket, and with a service that accepts and sends nothing, `build_queue.py`, `build_queue.py 4` and `build_why.py` each exit 2 within 1 s with the stderr line; the pane shows `Build queue could not be read.` and keeps its card selection.
  - The token path in `memory_gate.sh` against an in-process server: admit, release, admit again gives two episodes, one `admitted` row each in `queue_times.jsonl`.

**Constraints from prior phases:**
- Phase 3: `admission_protocol.py`, `admission_service.py` and `admission_client.py` exist; `AdmissionUnavailable` is in `admission_protocol.py`, and `view`, `explain` and `mine` raise it within `QUERY_TIMEOUT_SECONDS` (0.5 s); `admit` and `launch` carry an episode id kept across reconnects and new for each acquire, mutations carry a `request_id`, and the service replays a recorded reply from the ledger in `state.json`; `AdmissionServer(state_dir, clock, readings, proc)`; `admission_test_support.py` is the one in-process server fixture; the client's Python API (`admit`, `launch`, `release`, `width`, `give_back`, `view`, `explain`, `mine`, `priority`, `admit_head`); `reservation_stem`, `step_estimate` and the pure `QueueInputs` builder in `memory_admit.py`; the shadow source, its episodes and `admission/shadow/`, which this phase deletes; the restart rule of its Decided block; the units (socket mode 0600, `Type=notify`, `WatchdogSec=30`, `ConditionPathExists=`).
- Phase 2: `admission_pass` is the one decision, one walk per input scenario; `queue_decision` is gone; `QueueExplanation.decided_from`; `blocking_index` and `blocker_identity`; `next_recovery`; `EstimateExpired`.
- Phase 1c: `queue.json`, its writer and `queue_read`'s snapshot path (deleted here); the `QueueInputs` serial form and `queue_read_from` stay; the card and the renderer read `QueueRead`; the pane's `QueueSelection`.
- Resources: the phases that land before this one: through resources 27 (its Decided block). Their rules and estimates stay in `memory_admit.py`, and the service runs them.

**Acceptance gate:**
- Before the cut-over, the full merge gate as `docs/plans/builds-production.md` names it, green.
- `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY python3 -m unittest scripts.lint.test_memory_admit scripts.build_queue.test_admission_service scripts.build_queue.test_admission_client scripts.build_queue.test_build_queue scripts.build_queue.test_build_why scripts.build_queue.test_queue_view scripts.direct.test_verify_hold_memory scripts.direct.test_verify_token_reservation scripts.direct.test_verify_tree_cancel_reacquire scripts.direct.test_verify_checks_fail_closed scripts.direct.test_verify_lint_guard_key scripts.hooks.test_brp_launch_gate scripts.build_hold.test_build_hold scripts.build_hold.test_hold_shortcut scripts.buildlog.test_mac_idle scripts.shutdown.test_bounded_stop scripts.production.test_merge_gate_suites_admission` green.
- `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python` over each slot's changed Python files: `0 errors, 0 warnings, 0 notes`.
- `grep -n "flock" scripts/lint/memory_admit.py scripts/lint/memory_gate.sh scripts/build_queue/admission_*.py` finds nothing. `grep -n "\.waiting\|\.reservation\|admission/lock\|queue\.json"` over the Files' code files finds only the one import in `admission_service.py`.
- Before the cut-over, `admission_service.py drain-check` prints `drained` with the hold in force; the summary gives its output.
- Live after the cut-over, at load above 100: 50 timed calls each of `admission_client.py view`, `build_queue.py`, `build_queue.py 4` (the card) and `build_why.py`, all under 1 s, p95 under 0.25 s. The summary gives the times.
- 24 h after the cut-over: no earlyoom kill caused by admission (earlyoom's journal); the median and p90 of `steps.mem_wait_s` against the 24 h before; every step row with a call id has an `admitted` row in `queue_times.jsonl`. The summary gives the numbers.
- The notice has `also touches` lines for `memory_admit.py`, `test_memory_admit.py`, `memory_gate.sh`, `pre-tool-use-brp-launch-gate.sh`, `build_hold.py`, `override_guard.py`, `override_guard.sh` and their tests (resources-unit reviewed the diff; trial-merged against `origin/builds-resources`); `invoke.sh`, `verify.sh`, `mac_idle.py` and their tests (verify-unit, against `origin/natedev-verify`); `merge_gate.py`, `stop.py` and their tests (the natedev showrunner, against `origin/natedev`); `push_catalog.py` and `queue.test.ts` (natedev messaging-unit and unit-board-unit, against `origin/natedev`); and `outage_alerts.py` if it changed (its owner as the builds showrunner names it).
- Change-Review line: builds wait on the queue service, and the polling admission is gone | on natedev while steps wait, type `/build:queue`, then run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/admission_client.py view` | the queue answers in under a quarter second, and each waiting step's output shows the same wait line as before.
