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

**Carried over** from report-unit Phase 4g (handover 2026-10-30 11:3x EDT). Report-unit's two workers started Part A at about 11:20 EDT and stopped mid-turn. Their draft edits are in `4g_partA_v2.patch` in the run's handover folder, `/tmp/claude/unit/2ac94b9b-8a9d-4a9b-b908-b4f87dd4d5d0/handover/` (`git diff --binary` against `builds-report` `e27c243c`; `git apply --3way`): `queue.md`, `queue.tsx`, `register.tsx`, `queue.d.ts`, `build_queue.py`, `head_facts.py`, `roster.py`, new `queue_card.py`, `queue_fixture.py`, `queue_lines.py`, `queue_view.py` and `scripts/message/unit_labels.py`. It is unreviewed and untested: the writer starts from it, reads all of it against the Spec and keeps only what matches. The design notes, mocks, measurements and report-unit's worker prompts are in the same folder.

**Depends on:** Part A1 depends on nothing. Part A2 depends on report-unit 4d and 4e on `origin/builds` (the draft builds on `head_facts.py` from 4e; report sends one hash after its 4c lands, about 13:00 EDT); merge `origin/builds` first. Part B moved to Phase 1c (showrunner, 2026-10-30: Part A and Phase 1b land as one checkpoint; Part B lands on its own after resources Phase 30).

**Order of work:** resources-unit's unmerged work (its phase 25, then Phase 30) also changes `memory_admit.py`, `test_memory_admit.py`, `build_why.py`, `test_build_why.py`, `build-memory-admission.md` and `build-targets.md`, and a merge cannot land on uncommitted edits to those files. Part A, before that merge: every other file, in two steps (showrunner, 2026-10-30: start every item that does not need `head_facts.py` now). A1, now: `queue_lines.py`, the `unit_labels.py` move out of `roster.py`, the pane (`queue.tsx`, the build queue parts of `register.tsx`, `queue.d.ts`, `queue.test.ts`) and `queue.md`. A2, after the unit director merges `origin/builds` with report-unit 4d and 4e: `head_facts.py`, `queue_view.py`, `queue_card.py`, `queue_fixture.py`, `build_queue.py` and the golden files. In Part A `build_queue.py` keeps `locked_queue_read()` and the existing `LineWaiter`, and `queue_lines.py` gets its own cell width. Part B: Phase 1c.

#### Work Order

**Goal:** `/build:queue` prints one line per step at every width, says once why the steps wait (with each check for #1 as ✓/✗ and its numbers), marks each moved-up step with who moved it and from where, gives a `starts` estimate, and `/build:queue <position|session|folder>` prints one step's card. The view, build-why and the card each answer in under 1 s.

**Spec:**

User requirements (2026-10-30, verbatim): "build queue is unusable for me ... i can't parse this ... it is acceptable to be wider ... be more creative ... it is TOO slow"; "i need instantaneous answers on builds ALWAYS ... i cannot get clarity"; "i need to get sub second responses to any questions like these"; "only I can bump". Bump priority comes only from the user's `/showrunner:bump`, scoped to that target, for the time the user names; resources-unit builds that enforcement, not this phase.

Measured (natedev, 2026-10-30 10:30–11:00 EDT, load 130–184 on 32 cores): `build_queue.py` takes 3.3–6.1 s. The full roster build takes 3.9 s for about 28 labels; `roster.unit_labels_for` gives the same labels in 0.02–0.12 s. The shared admission lock wait is 0.13–7.24 s (median 2.07 s): about 6 `check` passes per second hold `LOCK_EX` 77% of the time. Imports cost 0.22–0.54 s. The pane is about 89 columns; the waiting table needs about 256, so `queue.tsx` prints about 370 `Label: value` lines for 32 steps.

1–3. Part B: moved to Phase 1c.
4. **Labels and imports.** `build_queue.py` reads the queue first (`queue_read()`), then makes one `roster.unit_labels_for(ids, os.environ)` call with every id it shows: running and waiting session ids, bump actors, token-wait sessions (`head_facts.session_ids`). `read_roster`, `read_queue_and_roster`, the thread pool and the `Roster: …` problems line go. `build_why.py` uses `queue_read()`. Module-level imports in `scripts/build_queue/*` are only what the read path uses; the bump path and argparse-only modules import inside their functions. `roster.unit_labels_for` and the file-only helpers it uses move from `scripts/message/roster.py` into the new `scripts/message/unit_labels.py`, which imports no herdr, role_map or design_roles; `roster.py` imports them from it, so there is one copy (showrunner approved 2026-10-30; the notice carries an also-touches line for shutdown-unit, trial-merged against its tip).
5. **One renderer.** New `scripts/build_queue/queue_lines.py`: `Span(text, tone, bold)` with `Tone = Literal["default", "success", "error", "warning", "inactive"]` (the mod's own tone names), `Line = tuple[Span, ...]`, terminal cell width (move `build_why._terminal_width` here; build_why imports it), right/left column padding, `cut_tail(text, cells)` with `…`, word wrap with a hanging indent, `plain(lines) -> str` and `json_form(lines, columns, as_of)`. `queue_view.render_view(model, columns) -> list[Line]` and `queue_card.render_card(model, matches, columns) -> list[Line]` produce every line. `build_queue.py [--columns N] [--json] [<selector>]` and `build_queue.py bump <selector>`: no word prints the view, one word prints the card, `bump` keeps its report-unit 4a and Phase 1 behavior and refusal (exit 3). `--columns` defaults to `COLUMNS` when it is a positive integer, else the stdout terminal width when stdout is a terminal, else 100. Text output prints `plain(lines)` (no Markdown table, so Claude Code cannot turn it into records). `--json` prints `{"columns": N, "as_of": <epoch>, "lines": [[{"text", "tone", "bold"}]]}`. No `--wide` mode: the card holds every fact of one step (unit director's call; a one-line revert of the Spec).
6. **The view (Mock A).** Order of lines, at 100 columns, from the 10:46 queue (the full 32-row form is the tester's golden file):

   ```text
   Build queue 10:46:12 · 1 running · 32 waiting · #1 waits on memory pressure, budget
   Waiting by production: tool-based-ui 19 · cargo-liner 6 · performance 4 · cargo-liner-style 3

   Running
                ran    using  step      held  production · unit · session
   ●    ↑29   0 min      5.1  nextest   11.1  tool-based-ui · widget · widget-enhancements-title

   Waiting · moved-up steps (↑) go first, newest move first; then the longest wait first.
             A long wait alone does not make a step next.
     ↑ #1 moved up: from #27 by work-lead at 10:40; from #1 by work-lead at 10:46
     ↑ #2 moved up: from #27 by work-lead at 10:40
     ↑ running nextest moved up: from #29 by widget at 10:43

      #      waited   starts  step     needs  production · unit · session
   ■  1 ↑27   6 min   ~1 min  nextest   11.5  tool-based-ui · cables · work-lead
            #1 waits on  ✗ memory pressure: full avg10 7.7 now; starts below 5.0
                         ✗ build budget: 11.1 of 21.0 GiB held; needs 11.5; starts when held falls to 9.5
                         ✓ free memory 24.5 GiB; needs 20.5: this step 11.5 + earlyoom 3.0 + promised not yet used 6.0
            #2-#32: strict until 10:56:00 (9 min 48 s left): younger steps wait; 4 of 31 fit now
      2 ↑27   6 min   ~3 min  nextest   11.5  tool-based-ui · cables · work-lead
      3      39 min   ~4 min  mend       7.6  tool-based-ui · hana-migration
      4      37 min   ~5 min  clippy     2.9  cargo-liner · berth · cargo-liner-berth-parallel-impl

   Memory in GiB. starts: an estimate at the last hour's pace, 44 steps an hour.
   One step: /build:queue 4 (or a session name or folder)
   ```

   - Line 1: as-of time (`HH:MM:SS`, user zone), counts, then the head's state: `#1 waits on <causes>` (`memory pressure` or `recovery spacing`, `budget`, `free memory`, from the head's decision and checks), `#1 starts at the next check` (decision admit), `nothing waits`, or `memory reading unavailable`. Line 2: waiting count per production (showrunner label), most first, ties by name; `—` counts as `other`.
   - Running columns: `●` (success), `↑<old place>` when matched to a move (rule below), `ran` (minutes since `admitted_at`, `—` when not recorded), `using` (measured, `—` when starting), `step`, `held` (the charge admission counts: `ExplainedRunningStep.charged`), name.
   - Waiting columns: `■` on #1 (error) else blank, `#`, `↑<old place>`, `waited` (`<n> min`), `starts`, `step`, `needs` (one decimal), name. Numbers right-aligned, text left-aligned, measured in terminal cells. Waits of 30 min or more: warning tone.
   - Name: `production · unit · session` from `UnitLabels`; a part equal to an earlier part is left out; an unmatched step shows its recorded session name, else its folder name. The session part is `inactive` tone.
   - The `#1 waits on` block: the head's pressure, budget and free checks from `HeadExplanation` (✓ success, ✗ error). Pressure forms: full avg10 (limit `FULL_PRESSURE_LIMIT`), swap with some avg10 (`swap <p>% used and some avg10 <s>; starts when swap is at most 85% or some avg10 at most 25.0`, from `SWAP_USED_LIMIT` and `SOME_PRESSURE_LIMIT`), recovery spacing (`one start per 30 s; next start at HH:MM:SS`), no hold (`✓ memory pressure: no hold`). Budget uses `charged`, `budget`, `need`, `starts_at`; a run-alone pass adds `(runs alone)`. Free uses `available`, `threshold` and its parts (`need`, `reserve`, `growth`, `head_need` when not 0). The same check-line function serves the card; no caller repeats admission arithmetic.
   - report-unit 4e facts only when known: `head_facts.head_time_text` as a block line only for `RecordedHeadTime`; a known cargo-token wait of the head as one more block line (`cargo token: waited <n> s (HH:MM-HH:MM)`). `head for: unknown`, `bumps: unknown`, `no bumps since it joined`, `cargo token: unknown` and `no cargo token wait` print nothing. build-why keeps its full report-unit 4e sections.
   - The rest, said once: in a strict window, one line `#2-#<N>: <head_facts.order_text>; <fitting> of <count> fit now` (`WaitingLine`). Else one line per reason kind among #2–#N, in order of first place: places as runs (`#3 #5-#7`), then a short phrase from one `reason_phrase(reason)` (`build budget`, `free memory`, `memory pressure`, `recovery spacing`, `behind #<k>, an older step that fits`, `starts at the next check`, `memory reading unavailable`). Every `WaitReason` variant, the ordered-walk `BehindOlderFit` included, has an explicit `case`.
   - Width rules (one rule set; a step never becomes a record): 100 and up as above; 80–99 names drop the production part (line 2 keeps it); under 80 drop the `starts` column, then cut name tails. Only the name's tail is cut on a step row; when the fixed columns alone pass the width, the row is cut at the edge with `…`. Header, check, move and footer lines wrap with a hanging indent.
7. **Moves.** `head_facts` reads `queue_bump` rows once, back to the oldest waiting step's `since`, as `BumpRecord(at, actor, session, step, folder, old_position)` with report-unit 4e's scan rules and limits; `BumpHistory` (build-why's table) groups the same records with `at >= ` the head's `since`. A waiting row is moved up when its waiter row has `bump_order < 0` (admission's field). Its moves are the records with the same `session` (its session id, else session name), `step` and `folder`, and `at >= ` its `since`; `↑<n>` shows the earliest move's `old_position`. A running step is marked when a record matches its identity with `at` in the scan window and at or before its `admitted_at`. Move lines: one per moved-up step, newest move first, `↑ <place> moved up: from #<old> by <actor> at HH:MM[; …]` (place `#<n>` or `running <step>`); steps with the same move list share one line (`#1 #2`); at most 5 lines, then `and <k> more moved-up steps`. Actor: its `UnitLabels.session`; `user:<name>` shows `the user`. A moved-up row with no matching record shows `↑` and no line. A failed bump read shows no marks and no lines.
8. **`starts` estimate.** Pace = lines of `admission/anon_peaks.jsonl` (admission's release record, appended under its lock in `ended_at` order) for this host with `ended_at` in the last 60 min. Read it backward with head_facts' block reader and stop at the first line older than the window (torn line skipped; a read error gives no estimate). Measured 2026-10-30 13:46–14:46Z: 44 lines, equal to the gated step rows that ended in the build log. Place `p` starts in about `round(p * 60 / pace)` minutes, at least 1: `~<n> min` in the view, `~HH:MM` in the card. Pace 0 or unread: `—` and the footer says `starts: no step ended in the last hour`.
9. **The card (Mock B).** `/build:queue <selector>`: one card per matched waiting step (a session with two steps gets two cards, a blank line apart). Selector: `bump_selector`'s shown-name match, then `match_waiters`.

   ```text
   #4 of 32 · cargo-liner · berth · cargo-liner-berth-parallel-impl · clippy · 2.9 GiB
   Folder /home/natepiano/rust/cargo-liner/repo. Waited 37 min (since 10:09). Likely start ~10:51.

   Next? No. 3 steps are ahead of it: 2 moved up, 1 joined earlier.
     #1 ↑  tool-based-ui · cables · work-lead nextest  from #27 by work-lead at 10:40
     #2 ↑  tool-based-ui · cables · work-lead nextest  from #27 by work-lead at 10:40
     #3    tool-based-ui · hana-migration mend         joined 10:07, 2 min before it
     Moved-up steps go first, then the longest wait. It joined 2nd of the steps waiting now.

   Waits on
     ✗ #1's turn      strict until 10:56:00 (9 min 48 s left): younger steps wait
     ✓ build budget   11.1 of 21.0 GiB held; needs 2.9
     ✓ free memory    24.5 GiB; needs 23.4: this step 2.9 + #1 11.5 + earlyoom 3.0 + promised not yet used 6.0
     ✗ memory pressure full avg10 7.7 now; starts below 5.0

   Likely start ~10:51: 4th in line at 44 steps an hour, an estimate.
     Each step moved up ahead of it adds about 1 min.
   ```

   - The first line under the heading answers: `Next? Yes. It starts at the next check.` (decision admit), `Next? Yes, it is first. It starts when the checks below pass.`, or `Next? No. <k> steps are ahead of it: <x> moved up, <y> joined earlier.` (`x` = rows ahead with `bump_order < 0`). Rows ahead: at most 5, then `and <k> more`. The checks come from its `LineWaiter` (Spec 2) or, for #1, the `HeadExplanation`, through the view's check-line function; the order check comes from its reason (`BehindHead`, `BehindOlderFit`) or `✓ queue order`.
   - A selector that matches a running step prints one line: `<name> <step> runs now: started HH:MM (<n> min ago), using <x> GiB, held <y> GiB.` No match prints `No waiting or running step matches <selector>.` (exit 0). The card has no bump line.
10. **The pane.** `queue.d.ts`: `QueueLines = { columns: number; as_of: number; lines: { text: string; tone: Tone; bold: boolean }[][] }`; `QueuePaneState` `queue` holds `data: QueueLines` and `selector: string | null`. `queue.tsx` only paints: one `Text` per span, no layout, no record fallback (`tableLines` goes); a line wider than the pane is clipped at the edge. `register.tsx` (build queue parts only): `queueRun` passes `--json --columns <n> [selector]`, `n` = the last `bodyColumns` the queue render saw (module-level, 100 before the first render); when a render sees `bodyColumns` differ from `data.columns`, it asks one forced refresh (the `tablePane` busy guard joins repeats). `/build:queue <selector>` opens the pane on that card and refreshes it every 10 s; `/build:queue` shows the view; `close` and `bump` stay as they are. Load validation checks the new shape and tone names.
11. **`commands/build/queue.md`.** Description: `Show running and waiting build steps, or one step's place and why it waits. Args - `[<position|session|folder>]`.` Body: runs `build_queue.py $ARGUMENTS` and shows the output as is inside a `text` code block; describes the one-line rows, the `↑` marks, the `#1 waits on` block, `starts` as an estimate and the card. Bump: remove "Any session or the user may run it". New text: moving a step up is the user's, through `/showrunner:bump`; never run `bump` for a session.
12. **Docs.** `docs/as-built/unit-board.md` (the queue pane rows, the `QueueLines` contract), `docs/as-built/build-memory-admission.md` (`queue.json`, `queue_read`, `LineWaiter` checks, `match_waiters`), `docs/as-built/build-targets.md` (the `/build:queue` row only). Also correct two lines report-unit 4f left wrong: the index schema number in `build-memory-admission.md` (23, now 24) and the `rebuild_bin` call in `build-targets.md`.

**Files:**
- `scripts/message/roster.py` — owner shutdown-unit; the label helpers move out
- `scripts/message/unit_labels.py` — new; the file-only label helpers moved out of roster.py
- `scripts/build_queue/build_queue.py` — the read, labels, CLI words, `--columns`, `--json`
- `scripts/build_queue/queue_lines.py` — new: spans, cell widths, cut and wrap, plain and JSON forms
- `scripts/build_queue/queue_view.py` — new: Mock A
- `scripts/build_queue/queue_card.py` — new: Mock B
- `scripts/build_queue/head_facts.py` — `BumpRecord` scan to the oldest waiter, per-row moves, pace reader
- `scripts/production/unit_marks.py` — new: the file-only session unit marks moved out of `unit_lookup.py`, so the label lookup loads no herdr
- `scripts/production/unit_lookup.py` — imports the moved marks from `unit_marks.py`; outside Owns, showrunner approved 2026-10-30
- `scripts/production/production_units.py` — imports `unit_lookup` inside the one function that uses it; owner natedev, showrunner approved 2026-10-30
- `scripts/herdr/place.py` — new: `HerdrPlace`, a plain data type with no herdr import; owner herdr-unit, showrunner approved 2026-10-30
- `scripts/herdr/herdr.py` — imports `HerdrPlace` from `place.py`; owner herdr-unit, showrunner approved 2026-10-30
- `scripts/herdr/test_herdr.py` — its patch target follows the moved helper; owner herdr-unit, showrunner approved 2026-10-30
- `scripts/message/test_unit_labels.py` — new: the label lookup loads no herdr
- `mods/unit-board/tests/kit.ts` — one shared drawing inspector for the queue and accounts tests; owner natedev unit-board-unit
- `mods/unit-board/tests/accounts.test.ts` — uses the shared inspector; owner natedev unit-board-unit
- `scripts/build_queue/test_build_queue.py`, `scripts/build_queue/test_build_why.py`, `scripts/build_queue/test_head_facts.py` — changed tests
- `scripts/build_queue/test_queue_view.py`, `scripts/build_queue/test_queue_card.py` — new tests
- `scripts/build_queue/queue_fixture.py` — new: the fixed 10:46 queue as `QueueInputs`, bump records and pace, shared by the tests
- `scripts/build_queue/golden/{view_80,view_100,view_140,card_1,card_4}.txt` — new golden renders
- `mods/unit-board/hooks/queue.tsx` — paint only
- `mods/unit-board/hooks/register.tsx` — build queue parts only: columns, selector, validation (owner natedev unit-board-unit)
- `mods/unit-board/types/queue.d.ts` — `QueueLines`, pane state
- `mods/unit-board/tests/queue.test.ts` — pane tests
- `commands/build/queue.md` — Spec 11
- `docs/as-built/unit-board.md`, `docs/as-built/build-memory-admission.md`, `docs/as-built/build-targets.md` — Spec 12

**Workers:** 1 writer + 1 tester — the Spec and the two mocks fix every line, so the tester writes from them alone.
- `impl` — `memory_admit.py`, `build_queue.py`, `queue_lines.py`, `queue_view.py`, `queue_card.py`, `head_facts.py`, `build_why.py`, `queue.tsx`, `register.tsx`, `queue.d.ts`, `queue.md`, the three docs; hub: `scripts/lint/memory_admit.py` (resources-unit reviews it)
- `test` — `queue_fixture.py`, the golden files, `test_queue_view.py`, `test_queue_card.py`, the changed tests in `test_build_queue.py`, `test_build_why.py`, `test_head_facts.py`, `test_memory_admit.py`, and `queue.test.ts`:
  - Golden renders of the fixture at 80, 100 and 140 columns, byte for byte, plus: no line wider than the columns except a cut step row; one line per step at 40, 60, 80, 100 and 140 columns; `starts` gone under 80; production part gone at 80–99.
  - Card answers: #1 (`Next? Yes, it is first.`), #4 (`Next? No. 3 steps are ahead of it: 2 moved up, 1 joined earlier.`), a decision-admit head, a session with two waiting steps (two cards), a running step line, no match.
  - Moves: earliest `old_position` on the mark, a repeat move on one line, shared move lists, the 5-line cap, a moved-up row with no record, a bump record older than the row's `since` not matched, a failed bump read.
  - Rest lines: strict window line; mixed reasons grouped by kind with place runs; `BehindOlderFit`.
  - Unknown report-unit 4e facts print no line; known head time and token wait print one line each.
  - Pace: window edge, torn last line, no lines (`—` and the footer), read error.
  - Timing: a fake `roster.roster` that sleeps 5 s and a counting `unit_labels_for`: the view and the card finish under 1 s, `roster.roster` is never called, `unit_labels_for` is called once per run with every shown id.
  - Pane: paints spans in order with their tones; no record lines at any width; a width change asks one forced refresh; `/build:queue 4` passes the selector; invalid JSON shows `Build queue could not be read.`


**Constraints from prior phases:**
- report-unit Phase 1: `tablePane` is the one pane lifecycle; a failed run shows only `Build queue could not be read.`; a bump refusal is `memory_admit.UnmatchedWaiter`, printed on stdout with exit 3, and the mod forwards only that. The report-unit Phase 1 binding that the head line keeps its `{"text", "tone"}` JSON field ends here: the pane and the CLI change together to `QueueLines`.
- report-unit Phase 3: release advice and check lines only print admission's answer; no caller repeats budget or free arithmetic. build-why uses `unit_labels_for`, never the full roster.
- report-unit 4a: `bump_selector` hands `WaiterRowSelector(row_key)` to `bump_waiter`; the race and prefix tests keep their assertions.
- report-unit 4e: `head_facts` is the one reader of bump rows and token waits and the one phrase source for head time, order window and token text; a fact with no record is unknown, never 0.
- Resources Phase 25: `HeadStartsAt` and `build_why.release_text` stay as they are.
- Resources ordered walk (Phase 30): `WaitReason` gains `BehindOlderFit`; `bump_waiter` refuses every caller (`/build:queue bump` prints `only the user can bump`, exit 2) until the user's `/showrunner:bump` path lands; waiter rows carry `head_state`/`head_since`; it may add a `give-back` exclusive section. Read its merged code before writing; where it differs from this line, its code holds.
- report-unit 4f (report's phase review, 2026-10-30): `scripts/buildlog/record.py` calls `memory_admit.git_identity` and `RepositoryIdentity` on every build step, and `index.py` loads `memory_admit` through `record`, so a `memory_admit.py` change reaches the build recorder too.
- The mod runs the installed `~/.claude/scripts`; a live pane check runs only after the change lands on main.
- Assertion changes in `scripts/build_queue/test_*.py`: an assertion on the old table text, `QueueSnapshot` keys, the head-line text, the `Roster:` problems line or the roster thread changes to the new form. For each changed or deleted assertion, the summary gives the test name, old → new and the Spec item that requires it. Assertions on admission facts (order, numbers, row keys, refusal text and exit 3, the bump race cases, report-unit 4e scan limits) do not change, except the read function they patch (`locked_queue_read` → `queue_read`).

**Acceptance gate:**
- `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY python3 -m unittest scripts.build_queue.test_build_queue scripts.build_queue.test_build_why scripts.build_queue.test_head_facts scripts.build_queue.test_queue_view scripts.build_queue.test_queue_card scripts.lint.test_memory_admit scripts.build_targets.test_build_targets scripts.message.test_roster scripts.buildlog.test_record scripts.buildlog.test_parse scripts.buildlog.test_test_phase_peak` green.
- `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python` over each slot's own changed Python files: `0 errors, 0 warnings, 0 notes`.
- `command claude plugin test mods/unit-board` green; `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/unit-board` clean.
- The notice has `also touches` lines for `memory_admit.py` and `test_memory_admit.py` (resources-unit, against `origin/builds-resources`; resources-unit reviewed the diff) `register.tsx` (natedev unit-board-unit, against `origin/natedev`) and `scripts/message/roster.py` (shutdown-unit, against its branch tip).
- Change-Review line: /build:queue shows one line per step and answers one step at once | on natedev while steps wait, run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_queue.py`, then the same with `4` | one line per step with ↑ marks and a `#1 waits on` block, and a card whose first line answers `Next?`, each in under a second.

### Phase 1b — The queue view renders each row once, aligns past place 99, and sends whole spans  · status: done
**Depends on:** Phase 1
**Estimate:** about 1 h of worker time.

#### Work Order

**Goal:** four small render defects the Phase 1 Part A2 review found after the hard landing (repair round 3). None changes an answer the view gives today.

**Spec:**
1. **One move lookup per row.** `head_facts` indexes the bump history once per model by step identity; `queue_view.move_lines`, `move_mark` and the card reuse each row's moves. Measured: 4,000 rows and 5,000 unrelated records took 0.915 s against 0.149 s with none. `step_labels` builds its fallback only when the label is missing.
2. **A later join is said as later.** In the card's steps-ahead list, a moved step with no move record that joined after the selected step says `joined HH:MM, N min after it`, never `0 min before it` (`queue_card.py` line 66).
3. **Columns hold past place 99.** The view sizes its place column from the largest shown place, so rows #99 and #100 put the names at one column (`queue_view.py` line 285).
4. **Whole spans.** `queue_lines.wrap` merges neighbouring spans of equal tone and bold as each row closes. Measured live: `--json` was 37,140 bytes against 3,939 bytes of text, and the pane painted one `Text` per word.

**Acceptance:** a test per item: a render with 5,000 unrelated bump records stays within 10% of one with none; the later-join text; equal name columns at #99 and #100 at 140 columns; a one-tone wrapped line gives one span per row. The five golden renders are unchanged as text. Live `build_queue.py --json` is at most 3 times the text size: measured live 9.4 times with one span per word (37,140 bytes against 3,939), 2.3–2.6 times with whole spans; every span keeps its `tone` and `bold` keys so the pane needs no defaults.

### Phase 1c — /build:queue reads a lock-free snapshot and answers in under a second every time  · status: todo
**Depends on:** Phase 1, and resources-unit's ordered-walk checkpoint (resources Phase 30: the queue starts steps in queue order, and every bump is refused until the user's `/showrunner:bump` path lands) on `origin/builds`; merge `origin/builds` first.
**Estimate:** about 2 h of worker time.

Phase 1 Part B, split out so Part A and Phase 1b land as one checkpoint (showrunner, 2026-10-30). Phase 1 Part A measured 0.4–1.2 s live at load 100–135 (3 of 12 runs over 1.0 s); the admission lock wait is the rest.

#### Work Order

**Goal:** `/build:queue`, its card and build-why read the queue from a lock-free snapshot, so every run answers in under 1 s; the card's checks behind #1 and its selector come from admission.

**Bridge to the service:** the `queue.json` snapshot (Spec 1) is how the view answers in under 1 s while polling admission stays. The service design says whether the service's view command replaces it; the view, card and renderer (Spec 4–11) read whichever source the design picks.

**Spec:**

1. **Queue snapshot (one lock-free read).** `memory_admit.py` edits start only after resources-unit's ordered-walk checkpoint is on origin/builds; resources-unit reviews the memory_admit.py diff.
   - Split `read_queue_pass` into `gather_queue_inputs(directory, processes, now, memory: Mapping[Path, StepMemory] = {}) -> QueueInputs` (every file and `/proc` read it does today: waiter rows with their file names, reservation rows with their measured memory and resolved identity, sccache memory, the meminfo reading or unavailable, pressure after recovery, `now`) and a pure `queue_read_from(inputs) -> QueueRead` (no file read). `read_queue_pass` calls both, so admission, the locked read and the snapshot reader run one decision path. `memory` lets a caller pass readings it already took; a missing path is read.
   - `write_queue_snapshot(directory, inputs)` writes `admission/queue.json` (`{"v": 1, "written_at": <epoch>, "inputs": ...}`) through `queue.json.tmp` and `os.replace`, no fsync (the file is a cache; an unreadable file reads as absent). The serial form and its parser live in `memory_admit.py` only; writer and reader import the same code.
   - One helper calls it at the end of every `LOCK_EX` section: `check`, `bump_waiter`, `release`, `set_width`, and any exclusive section the ordered-walk checkpoint adds (its `give-back`). The write runs after the section's own changes, on success and on an exception path. `check` passes the process table and the memory readings it already took. A failed write (OSError, ValueError) never changes the section's result or exit code; the old file stays and goes stale.
   - `queue_read() -> QueueRead`: parse `queue.json`; when `0 <= time.time() - written_at <= SNAPSHOT_MAX_AGE_SECONDS` (10) and the version and shape are valid, return `queue_read_from(inputs)`; else return `locked_queue_read()`. With waiters, their polls rewrite the file every few seconds; with no waiters nobody polls, so the lock is free. The reading's `now` is the snapshot's `now`; the view shows it as the "as of" time.
   - Moving the `/proc` scan out of the lock and fewer `check` passes stay with resources-unit (not in this phase).
2. **Per-step checks from admission.** `read_queue_pass` already computes each waiter's own `queue_decision`. Keep its numbers: `LineWaiter` gains `decision: AdmissionDecision`, `budget: BudgetCheck` and `free: FreeCheck`, built by the one function that builds the head's `BudgetCheck` and `FreeCheck` today (no second construction). `fits_now` stays.
3. **One selector matcher.** `memory_admit.match_waiters(selector: str, view: QueueView) -> list[int]` holds the match rule that `bump_waiter` uses today (position, session id, session name, folder, folder name). `bump_waiter` calls it under the lock; the card calls it on the snapshot reading. `build_queue.bump_selector` (shown names, report-unit 4a) stays the front of both.
4. **The read.** `build_queue.py` and `build_why.py` change from `locked_queue_read()` to `queue_read()`. The card's selector uses `match_waiters` in place of `queue_card.select_waiters`' own match rule (`queue_card.matches_step` goes), and its budget and free checks behind #1 come from each step's `LineWaiter` checks through the view's check-line function.
5. **One cell width.** One cell width for every Python caller (showrunner, 2026-10-30): `terminal_width` moves to the new `scripts/lib/terminal_cells.py` (a combining mark counts 0, a wide character 2), and `queue_lines.py`, `build_why.py` and `scripts/production/dailies_render.py` import it; `queue_lines.py` and `build_why.py` keep no width function and `dailies_render.display_width` is deleted. dailies_render.py is outside queue-unit's Owns: the showrunner approved the edit; claim it on berth (a holder goes to the showrunner), run `scripts.production.test_dailies_render`, and the notice carries an also-touches line trial-merged against its owner's tip, the switch to `queue_read()`, the docs for those files and the live timing.
6. **Docs.** `docs/as-built/build-memory-admission.md` (`queue.json`, `queue_read`, `LineWaiter` checks, `match_waiters`), `docs/as-built/build-targets.md` (the `/build:queue` row only). Also correct two lines report-unit 4f left wrong: the index schema number in `build-memory-admission.md` (23, now 24) and the `rebuild_bin` call in `build-targets.md`.

**Files:**
- `scripts/lint/memory_admit.py` — `gather_queue_inputs`, `queue_read_from`, `write_queue_snapshot` and its calls in every `LOCK_EX` section, `queue_read`, `SNAPSHOT_MAX_AGE_SECONDS`, `LineWaiter` checks, `match_waiters` (owner resources-unit; it reviews this diff)
- `scripts/lint/test_memory_admit.py` — snapshot and matcher tests (owner resources-unit)
- `scripts/build_queue/build_why.py` — `queue_read()`, cell width from `scripts/lib/terminal_cells.py`
- `scripts/lib/terminal_cells.py` — new: `terminal_width`, the one cell width
- `scripts/lib/test_terminal_cells.py` — new: its test
- `scripts/production/dailies_render.py` — imports `terminal_width`; `display_width` deleted; outside Owns, showrunner approved 2026-10-30
- `scripts/build_queue/build_queue.py`, `scripts/build_queue/queue_card.py` — `queue_read()`, `match_waiters`, `LineWaiter` checks
- `scripts/build_queue/queue_lines.py` — imports `terminal_width`
- `docs/as-built/build-memory-admission.md`, `docs/as-built/build-targets.md` — item 6

**Workers:** 1 writer + 1 tester.
- `test` — the snapshot, matcher and check tests:
  - Snapshot: a written file round-trips to the same `QueueRead` as the locked read; age 10 s uses it, 10.1 s and a future time take the locked read; wrong `v`, bad JSON and a missing file take the locked read; each of `check`, `bump_waiter`, `release`, `set_width` rewrites it; a failed write leaves the result and the old file; a reader never sees a partial file (temp + replace).
  - `match_waiters` gives the same matches `bump_waiter` used before; `LineWaiter` checks equal the head's construction for the same row.

**State:** `admission/queue.json` (new; rewritten under the admission lock at the end of every exclusive section; a cache that readers ignore when older than 10 s).

**Acceptance gate:**
- `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY python3 -m unittest scripts.build_queue.test_build_queue scripts.build_queue.test_build_why scripts.build_queue.test_head_facts scripts.build_queue.test_queue_view scripts.build_queue.test_queue_card scripts.build_queue.test_queue_lines scripts.lint.test_memory_admit scripts.lib.test_terminal_cells scripts.production.test_dailies_render scripts.message.test_unit_labels scripts.build_targets.test_build_targets scripts.buildlog.test_record scripts.buildlog.test_parse scripts.buildlog.test_test_phase_peak` green.
- `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python` over each slot's own changed Python files: `0 errors, 0 warnings, 0 notes`.
- Live on natedev, steps waiting, from the worktree's scripts: three runs each of `build_queue.py`, `build_queue.py --json --columns 89`, `build_queue.py 4` and `build_why.py`, every run under 1.0 s wall; the summary lists the 12 times and whether each read the snapshot. No screenshot.
- Live: the snapshot write adds at most 10 ms median to an exclusive hold over at least 200 `check` passes; the summary gives the median and the max.
- The notice has `also touches` lines for `memory_admit.py` and `test_memory_admit.py` (resources-unit, against `origin/builds-resources`; resources-unit reviewed the diff) and `dailies_render.py` (its owner, against its tip).

### Phase 2 — One decision pass decides for every waiting step, and the check and the queue read both call it  · status: todo
**Depends on:** Phase 1
**Blocked by:** resources-unit Phases 30, 31, 32 and 26 merged on `origin/builds` (32 is there at `8a71f222`; in its chain 24 Part B comes before 26). This phase lands before resources Phase 15. Tell resources-unit through berth when it starts and when it lands.
**Estimate:** about 6 h of worker time.

#### Work Order

**Goal:** one pure function, `admission_pass`, decides for every waiting step in one pass; `check` and `read_queue_pass` both call it, so a check and the view give the same decision for the same inputs. Builds see no change.

**Spec:**

Source: S1 of `docs/plans/builds-queue-service-design.md`, approved by the user at `93c86342` ("yes i want the build queue"). The design's line numbers are at `d27e24a1` and are out of date; find each function by its name in the merged code. Where the merged resources code differs from this Spec, its code holds and this Spec follows it.

1. **One input type.** The pass takes Phase 1's `QueueInputs` (from `gather_queue_inputs`). Do not make a second inputs type. Add to `QueueInputs` only what the pass needs and it lacks: the recovery state (`RecoverySpacing`, as `read_recovery` reads it), the admit-head override record (Phase 32), the queue priority record (Phase 26), the limits (`AdmissionLimits | ReadHostAdmissionLimits`) and `blockers_for: frozenset[str]` (owner keys `<pid>-<start_time>` whose blocker the caller wants). Phase 1's `queue.json` form and parser change with it, in the same place.
2. **The pass.** `admission_pass(inputs: QueueInputs, now: float) -> PassResult` in `memory_admit.py`. It is pure: no file read, no `/proc` read, no clock read, no write. It calls Phase 30's admission walk (the one function that walks the ordered waiting snapshot once and gives every row its decision); it never walks a second time. It also calls the functions that stay as they are: `decide`, `build_charge`, `charge_basis`, `growth_headroom`, `head_priority`, `queue_decision`, `head_window`, `head_wait_reason`, `head_wait_text`.
   - `PassResult` holds `rows: tuple[PassRow, ...]` in queue order, `head: HeadPriority | NoHeadPriority`, `totals: AdmissionDecision` (today's `decide(available, total, 0, memory)`), `recovery: RecoverySpacing` (the state after this pass), `head_marks` (the head-state and priority writes that Phase 26 and today's `check` make, as data), and `override: OverrideKept | OverrideEnded` (Phase 32's record: kept, or ended because its step was admitted or left).
   - `PassRow` holds `owner: str`, `decision: AdmissionDecision`, `reason: WaitReason`, `blocker: BlockingIndex | NoSingleBlocker` (computed only for owners in `blockers_for`), `admit_rank: int | NotAdmitted` (the order in which this pass admits).
3. **Rules the pass states once.** Today the 5 s poll gives these by accident; one pass must state them.
   - Every row the walk admits adds its need to the rows after it (Phase 30's marked needs).
   - In a recovery window (`RecoveryStarts`), the pass admits at most one row. The rows after it hold with the recovery pressure, and `PassResult.recovery` has `next_start_at = now + RECOVERY_START_SECONDS`.
   - Recovery changes move from `check` into one pure function `next_recovery(recovery, pressure, now) -> RecoverySpacing` (a pressure hold gives `PressureHoldRecorded`; a recorded hold that ended gives `RecoveryStarts(now + RECOVERY_WINDOW_SECONDS, now)`; an ended window gives `NoRecoverySpacing`). `check` and `recovery_pressure` call it, and the caller writes the result.
   - A row whose `estimated_at` is `HEAD_SECONDS` or more before `now` holds with a new reason, `EstimateExpired(estimated_at)`. It keeps its place and its room in the walk, so a younger row cannot take that room. Add it to `WaitReason`; every `match` on `WaitReason` gets an explicit `case` (`queue_view.reason_phrase`: `estimate refresh`; `build_why`'s reason text: the same words). `check` keeps its stderr line `memory admission: estimate expired while waiting for the lock; refresh on the next poll` when its own row gets this reason.
4. **Callers.**
   - `check` keeps its estimate outside the lock, the lock and its file writes. Under the lock it builds `QueueInputs` with `gather_queue_inputs` from the process table and memory readings it already took, adds its own waiter row, calls `admission_pass`, and reads its own `PassRow`. It then writes the head marks, the recovery state, the override end, and its waiter or reservation row, as today. `CheckResult` keeps every field; `head_need`, `head_label`, `head_reason` and `blocker` come from the pass. Its direct calls of `head_priority`, `queue_decision`, `reservation_blocker` and `head_wait_reason` go.
   - `queue_read_from` (Phase 1), so also `read_queue_pass`, builds `QueueView` and `QueueExplanation` from the `PassResult`: each `WaitingStep.reason` is its `PassRow.reason`, `HeadExplanation` comes from the head's row, and each `LineWaiter` check (Phase 1 Spec 2) comes from its row's decision. It makes no direct call of `queue_decision` or `head_priority`.
   - `reservation_blocker` splits in two. Its rule part, `blocking_index(...) -> BlockingIndex | NoSingleBlocker` (which running step's end would admit the row), is called only by `admission_pass`. Its identity part, `blocker_identity(index, reservations, processes, proc) -> Blocker` (`QueueIdentityReader`, the sidecar unit), is called by `check`, `blocker_snapshot` and `current_blocker`.
   - `head_after_steps_end` and `smallest_head_release` stay frozen and read-only (Phase 25, Phase 30). They call `admission_pass` on a changed copy of the inputs in place of `queue_decision`.
   - After this phase, `queue_decision(` is called only inside `admission_pass` and the walk, and in tests of `queue_decision` itself.
5. **No change for builds.** Every existing test passes with no assertion change. The only new text is the `EstimateExpired` phrase.
6. **Docs.** `docs/as-built/build-memory-admission.md`: one paragraph, "One decision pass": the inputs, the result, the three rules in Spec 3, and that the check and the view call the same pass.

No-copies rule (user, 2026-10-30): one place for each thing. A second copy of setup, a fixture, a config writer, a parser or a helper is a defect. A copy exists only with the user's own approval. Fix at the cause, never a patch at each site.

**Files:**
- `scripts/lint/memory_admit.py` — `admission_pass`, `PassResult`, `PassRow`, `next_recovery`, `EstimateExpired`, `blocking_index`, `blocker_identity`, the `QueueInputs` fields; `check`, `queue_read_from`, `head_after_steps_end`, `smallest_head_release` call the pass (owner queue-unit since 2026-10-30; resources-unit reviews the diff)
- `scripts/lint/test_memory_admit.py` — the pass tests
- `scripts/build_queue/queue_view.py` — `reason_phrase` case for `EstimateExpired`
- `scripts/build_queue/build_why.py` — the `EstimateExpired` reason text
- `scripts/build_queue/test_queue_view.py`, `scripts/build_queue/test_build_why.py` — the new reason
- `docs/as-built/build-memory-admission.md` — Spec 6

**Workers:** 1 writer + 1 tester — the Spec fixes the pass's inputs, result and rules, so the tester writes from it alone.
- `impl` — `memory_admit.py`, `queue_view.py`, `build_why.py`, `build-memory-admission.md`; hub: `scripts/lint/memory_admit.py` (resources-unit reviews it)
- `test` — `test_memory_admit.py`, `test_queue_view.py`, `test_build_why.py`:
  - One `QueueInputs` fed to the check path and to the view path gives the same decision, reason and blocker for every row, in each of: strict window, backfill window, no head priority, pressure, recovery spacing, `BehindOlderFit`, queue priority, admit-head override, reading unavailable, estimate expired.
  - The pass reads nothing: it runs with the live-memory tripwire armed and `BUILDLOG_PROC` set to an empty folder.
  - Two rows that fit in one pass both admit, and the second is charged with the first's need; in a recovery window only the first admits, and `next_start_at` is set.
  - An expired estimate holds with `EstimateExpired`, and a younger row that fits behind it holds with `BehindOlderFit`.
  - `queue_view` and `build_why` print `estimate refresh` for it.

**Constraints from prior phases:**
- Phase 1: `gather_queue_inputs`, `queue_read_from`, `queue.json` and its one parser, `queue_read`, `LineWaiter` checks and `match_waiters` are in `memory_admit.py`; `queue_view.reason_phrase` has one explicit `case` per `WaitReason` variant.
- Resources Phase 30: one admission walk in queue order, `BehindOlderFit`, the bump stop. Phase 31: Rule A is one more check in that walk. Phase 32: `admit_head`, `end_head_override`, `require_showrunner`. Phase 26: queue priority by unit (`queue_priority.json`, `priority set|clear`), `head_state`/`head_since`, the give-back handoff and `queue_times.jsonl`; `bump_order` and `holds_younger` are gone. Phase 24 Part B: `LifetimePromise`, `finalize_reservation`, `reservation_lifetime`. Phase 25: `HeadRelease`, `build_why.release_text` is the one writer of the release line.
- Resources Phase 15 (after this phase) changes `reservation_memory` and the charge, not the pass; it lands on this pass.

**Acceptance gate:**
- `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY python3 -m unittest scripts.lint.test_memory_admit scripts.build_queue.test_build_queue scripts.build_queue.test_build_why scripts.build_queue.test_queue_view scripts.build_queue.test_queue_card scripts.production.test_merge_gate_suites_admission scripts.buildlog.test_record` green, with no changed assertion in an existing test.
- `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python` over each slot's changed Python files: `0 errors, 0 warnings, 0 notes`.
- `grep -n "queue_decision(" scripts --include=*.py -r` shows calls only in `admission_pass`, the walk and tests.
- The notice has `also touches` lines for `memory_admit.py` and `test_memory_admit.py` (resources-unit reviewed the diff; trial-merged against `origin/builds-resources`). `build_why.py` is in queue-unit's Owns (`scripts/build_queue/`, a16bda0b), so it needs no line.
- Change-Review line: the check and the queue view decide with one pass | on natedev while steps wait, type `/build:queue` and read one waiting step's own wait line in its output | the reason the view gives each step is the reason its own step prints.

### Phase 3 — The queue service runs beside today's admission and logs what it would admit  · status: todo
**Depends on:** Phase 2
**Blocked by:** the two system units of Spec 9 are in in `/etc/nixos/modules/linux/development.nix` on natedev. Queue-unit sent Spec 9 and the `OOMScoreAdjust` question to the natedev showrunner on 2026-10-30 (builds showrunner ask). The service unit has `ConditionPathExists=`, so the units can land before this phase's code is on main.
**Estimate:** about 10 h of worker time.

#### Work Order

**Goal:** one service process, `admission_service.py`, holds the queue in memory and answers a socket protocol that a client and its tests use. Live, it runs as a shadow: it reads today's queue files and writes none of them, runs `admission_pass` every second with its own readings, and logs what it would admit. Builds see no change.

**Spec:**

Source: S2 of `docs/plans/builds-queue-service-design.md` (with §2 The service, §4 Commands, §5 When the service is down and §8 Tests), approved by the user at `93c86342` ("yes i want the build queue"). The design's line numbers are at `d27e24a1`; find each function by its name. The design's `bump {selector, actor}` request and its `bump_order` and `holds_younger` fields are out of date: resources Phase 26 deleted them. This Spec uses Phase 26's queue priority and give-back handoff, and Phase 32's `admit_head`. Where the merged resources code differs from this Spec, its code holds and this Spec follows it.

1. **Three new files in `scripts/build_queue/`.**
   - `admission_protocol.py`: the request and reply types, one encoder and one parser for each message, the version check, and `admission_socket_path() -> Path` (`ADMISSION_SOCKET` when set, else `/run/build-admission/build-admission.sock`). It also holds the one serial form of `QueueRead` and `QueueExplanation` for the `view`, `explain` and `mine` replies, as one writer and one parser. The service and the client import it; neither encodes or parses a message itself.
   - `admission_service.py`: `AdmissionServer`, its `Clock` protocol, the shadow source (Spec 7), and the `serve` and `report` commands.
   - `admission_client.py`: the client's Python API and its CLI (Spec 8).
2. **Shared rules move into one function each in `memory_admit.py` (no copy).**
   - Stem rule: `reservation_stem(directory: Path, pid: int, start_time: int, admitted_ns: int) -> Path` gives `admission/<pid>-<start>-<ns>`. `check` names its `.reservation` file from it, and `finalize_reservation` builds its `.max` and `.wave` paths from it. The service's reservation id is the stem's name, and the service sends the `.max` and `.wave` paths in `admitted`. `invoke.sh` :288 still builds `${BUILDLOG_MEM_RESERVATION%.reservation}.max` until Phase 4 replaces it with the path from `admitted`.
   - Estimate: the estimate part at the top of `check` (from `estimate_time` to the profile its locked part uses: `gate_estimate` for a `GateProfile`, the expiry of a cached `StepProfile`, and `git_identity`, `expected_peak` and `filtered_peak` for `InferFromArgv`) moves to `step_estimate(argv: list[str], cwd: str, profile: InferFromArgv | StepProfile | GateProfile, now: datetime, root: Path) -> StepProfile | GateProfile`. `check` calls it, and the client calls it before it connects. Behavior does not change.
   - Inputs: the service builds `QueueInputs` with the same code as Phase 1's `gather_queue_inputs`. If `gather_queue_inputs` reads files or `/proc` itself, split it: the reads stay in it, and one pure builder takes what it read. It and the service both call that builder.
3. **What the service holds.** One single-threaded loop on one `select.epoll`, with no threads and no locks. In memory:
   - waiters: the connection, the owner (pid, start time), the profile and need, `estimated_at`, `since`, Phase 26's `head_state` and `head_since`, the give-back handoff when the `admit` carries one, and the identity (session id, name, step, folder);
   - reservations: the owner, the scope cgroup path, the need, `admitted_at`, the width, the call id, the shape (resources Phase 24), the sidecar, and the head state copied at admission (Phase 26);
   - the queue priority record (Phase 26) and the admit-head record (Phase 32);
   - the recovery spacing and the last host reading (meminfo, PSI, sccache anon);
   - the last `PassResult`. `view`, `explain` and `mine` read it.

   Persisted: one file, `<state dir>/state.json`, written through a temporary file and `os.replace` after each change to the reservations, the priority, the admit-head record or the recovery spacing. Waiters persist only their order fields (`since`, `head_state`, `head_since`), keyed by owner `<pid>-<start_time>`. The form of `anon_peaks.jsonl`, `queue_times.jsonl` and the build log rows does not change. The shadow's state dir is `admission/shadow/` (Spec 7). Phase 4's live service uses `admission/` and imports today's files once when `admission/state.json` is absent; because the shadow keeps its state in its own folder, that import still runs.
4. **Start, restart and owners.**
   - At start the service reads `state.json`. For each reservation it checks the owner (pid and start time) and the scope (`cgroup.procs`) with `reservation_lifetime` (`PopulatedStepScope | LiveOwnerProcess | EndedStep`). Live rows keep their charge; ended rows go through `finalize_reservation`. Then it sends `READY=1` to `$NOTIFY_SOCKET`.
   - Each loop turn sends `WATCHDOG=1` to `$NOTIFY_SOCKET` when it is set.
   - The service takes its listening socket from systemd (`LISTEN_FDS`). It binds `admission_socket_path()` itself only when `LISTEN_FDS` is absent (tests, a run by hand).
   - A waiter ends when its connection closes or its owner exits. A reservation ends on `release`, or when its owner has exited and its scope is empty; a reservation with no scope ends when its owner exits. The service watches each owner with `os.pidfd_open`, and each scope's `cgroup.events` for `populated 0` (registered on the epoll with `EPOLLPRI`: kernfs wakes pollers when the file changes; read the file again after each wake). Both are kernel events, so no polling.
   - A connection never holds a reservation: a restart closes every connection and keeps every live reservation (design §2).
5. **Protocol.** Newline-delimited JSON on a Unix stream socket. One request per connection. Every request carries `"v": 1`. A request with an unknown `v` gets `refused {"reason": "client version N, service supports 1..M"}`, and the client stops with that line.

   | Request | Reply |
   | --- | --- |
   | `admit` {owner {pid, start_time}, argv, cwd, since, sidecar, identity {session_id, name, step, folder}, call_id, profile, handoff} | `wait` {reason, position, head, text} lines on each change, and at least every 60 s; then one `admitted` {reservation, need, source, max_path, wave_path} or `refused` {reason} |
   | `launch` {owner, identity} (BRP, no argv) | As `admit`. No reservation is made. |
   | `release` {reservation, status} | `ok` |
   | `width` {reservation, width} | `ok` |
   | `give_back` {reservation} | `ok` {handoff}: Phase 26's handoff. The reservation ends through `finalize_reservation`; the head state and `token_left_at` come back for the step's next `admit`. |
   | `view`, `explain`, `mine` {session_id} | One JSON answer from the last pass |
   | `priority` {action `set` or `clear`, unit, until, why, actor} | `ok` or `refused` |
   | `admit_head` {why, actor} | `ok` or `refused` |

   - `wait.reason` is the `WaitReason` name. `wait.text` is the phrase `queue_view.reason_phrase` gives it. In this phase the client prints `text` as it comes; Phase 4 moves today's wait line form from `memory_gate.sh` into the client.
   - On a `wait` with reason `EstimateExpired` (Phase 2), the client runs `step_estimate` again, closes, and sends a new `admit` with its first `since`.
   - The service writes the build log rows that the lock holder writes today (`anon_peaks.jsonl` at release, `queue_times.jsonl` through Phase 26's one append function, the priority rows), from the same functions.
   - The socket file is mode 0600, so only the user's processes connect. The service records `actor`. `require_showrunner` stays in the CLI that sends `priority` or `admit_head` (design §4).
6. **When a pass runs.** On every event: an `admit`, a close, a `release`, an owner exit, an empty scope, a `width`, a `give_back`, a `priority` or an `admit_head`. Also once a second while any waiter waits, because memory freed outside builds shows only in `/proc/meminfo`. A pass reads meminfo, PSI, sccache's `memory.stat` and each running scope's `memory.current` and `memory.swap.current` through the readers in `memory_admit.py` (`read_meminfo`, `read_pressure`, `sccache_memory`, `reservation_memory`). It reads the `/proc` tree only for reservations with no scope. It then calls `admission_pass` (Phase 2) and acts on the result: it admits rows in `admit_rank` order, writes the head marks and the recovery spacing, ends the override when the result says so, and sends a `wait` line to each waiter whose reason or position changed.
7. **The shadow, this phase's live run.** `admission_service.py serve` (the unit's `ExecStart`) runs the shadow. Its state dir is `admission/shadow/`; `--state-dir <dir>` changes it (the gate's run).
   - Waiters come from Phase 1's `queue.json` (its one parser). Reservations come from the `.reservation` rows, parsed by the function today's readers use. The shadow reads both each second with no lock and writes neither. Both are written through `os.replace`, so a read sees a whole file.
   - Each second it runs `admission_pass` on those rows with its own fresh readings and its own recovery spacing. A row it would admit counts in later passes as a reservation at its need until a real `.reservation` row for that owner appears or the owner leaves `queue.json`.
   - It appends rows to `<state dir>/decisions.jsonl` through the build log's one line appender (`store.append_line`): `{"at", "kind": "would_admit", "owner", "need", "snapshot_at", "snapshot_reason"}` the first time a pass admits an owner (`snapshot_reason` is the reason `admission_pass` gives that owner on `queue.json`'s own inputs, and `snapshot_at` is its `written_at`); and `{"at", "kind": "admitted", "owner", "admitted_at"}` when a `.reservation` row for an owner first appears.
   - It answers `view`, `explain` and `mine` from its own pass. It answers every other request with `refused {"reason": "shadow: today's admission decides"}`.
   - `admission_service.py report --since <ISO time> [--state-dir <dir>]` reads `decisions.jsonl` and prints four numbers and a verdict. Real admissions. Matched: sort the real admissions by `admitted_at` and the same owners by their `would_admit` time; a real admission matches when it has a `would_admit` row before its `admitted` row and has the same index in both lists. Held at the instant: `would_admit` rows whose `snapshot_reason` is `NeedsBuildBudget`, `NeedsMemory` or a pressure hold, with `snapshot_at` within 1 s of `at`. Median lead: `admitted_at` minus the `would_admit` time. The verdict is `adopt` when there are at least 200 real admissions, at least 99 % match, none is held at the instant, and the median lead is at least 2 s (design S2); else `not yet`.
8. **The client, `admission_client.py`.**
   - Python API: `admit(request: AdmitRequest, on_wait: Callable[[Wait], None]) -> Admitted | Refused`, `launch(...)`, `release(reservation: str, status: int)`, `width(reservation: str, width: int)`, `give_back(reservation: str) -> Handoff`, `view() -> QueueRead`, `explain() -> QueueExplanation`, `mine(session_id: str)`, `priority(...)`, `admit_head(why: str, actor: str)`. Phase 4's merge gate calls this API in process.
   - CLI: one subcommand for each request (`admit`, `launch`, `release`, `width`, `give-back`, `view`, `explain`, `mine`, `priority set|clear`, `admit-head`). `admit` prints each `wait.text` to stderr; on `admitted` it prints one protocol line to stdout and exits 0; on `refused` it prints the reason to stderr and exits nonzero.
   - When the service is down (design §5), the client never falls back to the lock. A closed connection: it connects again at once and sends its first request again, with its first `since`, and prints `admission service restarted; waiting again` once. No socket, or a refused connect: it tries again every 2 s and prints one line a minute. No `wait` line for 120 s: it prints `admission service not answering since HH:MM` (time in the user zone, through `user_zone.py`) and keeps waiting.
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
  - A restart with 30 waiters: each connects again with its `since`, and the order and `head_since` stay.
  - An owner killed while it waits leaves the queue at once; an owner with no scope killed while it holds a reservation releases it; a scope that outlives its shell keeps the charge until `populated 0`.
  - A client of version 2 against a service of version 1 gets `refused` with both versions.
  - A hung service (it accepts and sends no line): after 120 s of fake time the client prints `admission service not answering since HH:MM` and keeps waiting.
  - No socket: the client tries every 2 s, prints one line a minute, and never returns `Admitted`.
  - `give_back` returns Phase 26's handoff, and the next `admit` with it appends the `token_gap` row.
  - `priority` and `admit_head` change the next pass and record the actor.
  - `EstimateExpired`: the client estimates again and keeps its `since`.
  - The shadow, on fixture `queue.json` and `.reservation` files: it writes no file outside its state dir, logs `would_admit` and `admitted` rows, and refuses change requests; `report` prints the four numbers and the verdict for a fixture log.
  - `reservation_stem`, `step_estimate` and the `QueueInputs` builder each have a test, and every existing `check` test passes with no changed assertion.

**Constraints from prior phases:**
- Phase 2: `admission_pass(inputs: QueueInputs, now: float) -> PassResult` in `memory_admit.py` is the one decision for every waiting row. `PassRow` has `owner`, `decision`, `reason`, `blocker`, `admit_rank`; `PassResult` has `head_marks`, `recovery` and `override`. `next_recovery` is the one recovery change. `EstimateExpired` is a `WaitReason`. `blocking_index` and `blocker_identity` replace `reservation_blocker`.
- Phase 1: `queue.json` (`{"v": 1, "written_at", "inputs"}`) and its one parser, `gather_queue_inputs`, `queue_read_from`, `queue_view.reason_phrase`.
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
**Estimate:** about 21 h of worker time.

#### Work Order

**Goal:** every build step, BRP launch, merge gate, stop, view and command asks the service, all in one merge, and the same merge deletes the polling admission: `check`, the locks, the file readers, the poll loop, `.waiting`, `.reservation`, `admission/lock` and Phase 1's `queue.json` writer. A step starts only on an `admitted` answer, each step starts Python once instead of once every 5 s, and the view answers in under 0.25 s.

**Spec:**

Source: S3 of `docs/plans/builds-queue-service-design.md` (with §3 Decisions stay in one place, §4 Commands, §5 When the service is down and §8 Tests), approved by the user at `93c86342`. The design's views and commands phase (the former S4) is part of this phase, because a separate phase would keep the `queue.json` writer alive as a second copy of queue state. The design's line numbers are at `d27e24a1`; find each function by its name. Where the merged resources code differs from this Spec, its code holds and this Spec follows it.

1. **One cut-over, no second path (user, 2026-10-30 11:42 EDT).** Every caller moves in this phase's one merge. There is no mode file, no fallback to the lock and no second admitter: callers split between the lock and the service would be two admitters that cannot see each other's reservations, so both admit into the same free memory. The only rollback is a git revert of this phase, which brings back the Phase 3 shadow.
2. **The live service.** `admission_service.py serve` runs the live server with state dir `admission/`. The shadow source, `decisions.jsonl`, the `report` command and `admission/shadow/` go.
   - One import at start, only when `admission/state.json` is absent: each `.reservation` row (through `reservation_lifetime`: live rows keep their charge, ended rows go through `finalize_reservation`), each `.waiting` row's order fields keyed by owner, the queue priority record (`queue_priority.json`), Phase 32's admit-head record and the recovery spacing (`read_recovery`). The service writes `state.json`, then deletes the files it imported. A later start finds `state.json` and imports nothing.
   - Each admission appends one row to `queue_times.jsonl` through Phase 26's one append function: `{"at", "kind": "admitted", "session_id", "step", "pid", "start_time", "call_id", "need", "waited_s"}`. The 24 h check in the gate reads it.
   - The new code starts through Phase 3's restart rule (its Decided block).
3. **Cut-over steps.** The builds showrunner runs them at the merge to main, in a quiet hour. A running bash keeps the `memory_gate.sh` functions it read at its start, so a step that started before the merge would call the deleted `check`.
   1. `/build:hold hold "admission cut-over"` on natedev.
   2. Wait until `/build:hold quiet` reports no running or waiting step.
   3. Merge to main. The service restarts on the new code, imports, and answers.
   4. `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/admission_client.py view` answers from the live service.
   5. `/build:hold release`.
4. **Callers.**
   - `memory_gate.sh`: `buildlog_wait_for_memory` runs one foreground `admission_client.py admit ...` and reads its `admitted` line. `buildlog_release_memory` runs `admission_client.py release`, `buildlog_give_back_memory` runs `give-back`, and the width call runs `width`. Today's wait line form (`buildlog_memory_line_inner` and the `printf` lines in `buildlog_wait_for_memory`) and the leave-hook loop (`LINT_QUEUE_LEAVE_HOOK`, else `BUILDLOG_MAC_IDLE_HOOK`, run every 5 s; a success closes the connection and exits as `LeftQueue`) move into `admission_client.py`, one function each, and the shell copies go. The client prints the same lines as today from the `wait` messages, and `memory_gate.sh` keeps today's exit codes for admitted, refused and left.
   - `invoke.sh` :288: the `.max` path comes from the `admitted` line (`max_path`), not from `${BUILDLOG_MEM_RESERVATION%.reservation}`. `BUILDLOG_MEM_RESERVATION` holds the reservation id, not a file path. The sampler `buildlog_sample_anon` does not change.
   - `verify.sh`: the cancel path (:1171–1181) drops its reservation list, because the owner's end releases. The give-back call site keeps the name `buildlog_give_back_memory`.
   - `pre-tool-use-brp-launch-gate.sh`: runs `admission_client.py launch`.
   - `build_hold.py` `admitted_processes`: owners from `view`'s reservations. A service that does not answer raises the client's `AdmissionUnavailable`, and the callers treat it as they treat an unreadable admission folder today.
   - `override_guard.py` and `override_guard.sh`: `ADMISSION_SOCKET` joins `OVERRIDE_VARIABLES`, so the installed client refuses a socket outside the test's own folder, as it refuses `BUILD_HOLD_DIR` today. `admission_socket_path()` calls the guard; there is no second refusal check.
   - `merge_gate.py` `gate_reservation`: calls `admission_client.admit` and `release` in process (Phase 3's Python API) in place of `memory_admit.check`.
   - `stop.py` `_release_build_claims`: its admission part goes, because the service ends a stopped owner's waiter and reservation from its pidfd. Its `admitted(owner)` check asks `mine`.
   - `mac_idle.py` `waiting_evidence`: waiting steps come from `view`, not from `.waiting` files.
   - `build_why.py` asks `explain`. `build_queue.py` asks `view` and `mine`, and its priority and admit-head commands send `priority` and `admit_head` (`require_showrunner` stays in the CLI). `commands/build/queue.md` names the new source.
5. **Deletions in `memory_admit.py`.** `check`, `locked_queue_read`, `current_blocker`, `blocker_snapshot`, `live_waiters`, `live_reservations`, `queue_rows`, `cancel_waiter`, `release`, `set_width`, `write_admission_record`, the give-back CLI, `run_action`'s file protocol (it keeps only the actions with a caller outside admission, such as `tripwire`), every `flock`, Phase 1's `write_queue_snapshot`, the `queue.json` parser, `queue_read`'s file path, and the writers of the queue priority file and the admit-head file. The rules, estimates and readers stay (design §3): `admission_pass`, `decide`, `build_charge`, `charge_basis`, `growth_headroom`, `head_priority`, `queue_decision`, `head_window`, `head_wait_text`, `blocking_index`, `blocker_identity`, `head_after_steps_end`, `smallest_head_release`, `expected_peak`, `filtered_peak`, `gate_estimate`, `step_estimate`, `read_meminfo`, `read_pressure`, `reservation_memory`, `sccache_memory`, `queue_read_from`, `reservation_stem`, `finalize_reservation`, `reservation_lifetime`. The service is the only caller of `admission_pass`, directly or through `head_after_steps_end` and `smallest_head_release`.
6. **Down alert.** `OnFailure=` on `build-admission.service` starts `build-admission-alert.service` (`Type=oneshot`, written by the natedev NixOS session), whose `ExecStart` is `%h/.claude/scripts/lib/py %h/.claude/scripts/build_queue/admission_client.py down-alert`. The command waits until the service has been down for 2 min (the socket refuses, or no answer), takes a claim with `outage_alerts.claim("build-admission")`, sends `build admission service is down; builds wait` through `send.py`'s outage path with a new `push_catalog.py` entry, and calls `outage_alerts.delivered`. A claim that is in flight or delivered sends nothing, so there is one push for each outage. The service calls `outage_alerts.clear("build-admission", now)` after each start.
7. **Tests.** These 13 modules move to a per-test `ADMISSION_SOCKET` with the in-process server from `admission_test_support.py` (Phase 3): `test_verify_hold_memory`, `test_verify_token_reservation`, `test_verify_tree_cancel_reacquire`, `test_verify_checks_fail_closed`, `test_verify_lint_guard_key`, `verify_tree_cancel_test_support`, `test_brp_launch_gate`, `test_build_hold`, `build_hold_test_support`, `test_hold_shortcut`, `test_mac_idle`, `test_bounded_stop`, `test_merge_gate_suites_admission`. The merge-gate test runs start an in-process server in place of real check and release processes against fake state under `/dev/shm`. Tests of deleted functions go with them.
8. **Docs.** `docs/as-built/build-memory-admission.md`: rewrite the admission part for the service (what it holds, the protocol, release by pidfd and scope, the rule when it is down, the import, the restart rule) and delete the lock, `.waiting`, `.reservation` and `queue.json` parts. `commands/build/queue.md`: the new source.

No-copies rule (user, 2026-10-30): one place for each thing. A second copy of setup, a fixture, a config writer, a parser or a helper is a defect. A copy exists only with the user's own approval. Fix at the cause, never a patch at each site.

**Decided (showrunner, 2026-10-30): where this phase lands in resources-unit's chain.** Option A: after resources 27 and before 20 (chain 21 → 16 → 25 → 24 → 26 → 15 → 27 → **this phase** → 20 → 11 → 17 → 18 → 19 → 8). 27 is the one remaining resources phase that changes a caller this phase moves (`gate_reservation`), so landing after it moves that caller once; 20, 11, 17, 18 and 19 change rules and estimates that stay in `memory_admit.py`, which the service runs; 8 decides whether the service's `state.json` is shared build state. queue-unit confirms this place with resources-unit against its Work Orders.

**Files:**
- `scripts/lint/memory_admit.py` — Spec 5 deletions (owner queue-unit since 2026-10-30; resources-unit reviews the diff)
- `scripts/lint/test_memory_admit.py` — tests of deleted functions go
- `scripts/build_queue/admission_service.py` — the live server, the import, the `admitted` row; the shadow and `report` go
- `scripts/build_queue/admission_client.py` — the wait line form, the leave hook, `AdmissionUnavailable`, `down-alert`
- `scripts/build_queue/admission_protocol.py` — `admission_socket_path` calls the override guard
- `scripts/build_queue/build_why.py`, `scripts/build_queue/build_queue.py`, `commands/build/queue.md` — `explain`, `view`, `mine`, `priority`, `admit_head` (owner queue-unit)
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
- `scripts/build_queue/test_admission_service.py`, `scripts/build_queue/test_admission_client.py`, `scripts/build_queue/test_build_queue.py`, `scripts/build_queue/test_build_why.py` — the live service, the import, the client's lines, the down alert
- `docs/as-built/build-memory-admission.md` — Spec 8

**Workers:** 1 writer + 1 tester — the cut-over cannot split: callers split between the lock and the service are two admitters (Spec 1), so one writer moves every caller, and the tester writes from the Spec alone.
- `impl` — every code file above, `commands/build/queue.md` and `build-memory-admission.md`; hub: `scripts/lint/memory_admit.py` (resources-unit reviews it)
- `test` — the 13 moved modules, `test_memory_admit.py`, `test_admission_service.py`, `test_admission_client.py`, `test_build_queue.py`, `test_build_why.py`:
  - The import runs when `state.json` is absent and keeps each live reservation's charge; a second start imports nothing.
  - `memory_gate.sh`'s admit, release, give-back and width against an in-process server print today's lines and exit with today's codes; the leave hook exits as `LeftQueue`.
  - Each admission appends one `admitted` row to `queue_times.jsonl`.
  - The BRP launch gate, `admitted_processes`, `_release_build_claims`, `waiting_evidence` and `gate_reservation` each work against an in-process server.
  - The override guard refuses an `ADMISSION_SOCKET` outside the test's own folder.
  - `down-alert` with a fake send: one push for each outage, none while a claim is in flight or delivered, and the claim clears after a start.
  - The 13 modules pass on a per-test socket and never reach the live service.

**Constraints from prior phases:**
- Phase 3: `admission_protocol.py`, `admission_service.py` and `admission_client.py` exist; `AdmissionServer(state_dir, clock, readings, proc)`; `admission_test_support.py` is the one in-process server fixture; the client's Python API (`admit`, `launch`, `release`, `width`, `give_back`, `view`, `explain`, `mine`, `priority`, `admit_head`); `reservation_stem`, `step_estimate` and the pure `QueueInputs` builder in `memory_admit.py`; the shadow source and `admission/shadow/`, which this phase deletes; the restart rule its Pending decision picks; the units (socket mode 0600, `Type=notify`, `WatchdogSec=30`, `ConditionPathExists=`).
- Phase 2: `admission_pass` is the one decision; `blocking_index` and `blocker_identity`; `next_recovery`; `EstimateExpired`.
- Phase 1: `queue.json` and its writer (deleted here); `queue_read_from` stays; the card and the renderer read `QueueRead`.
- Resources: the phases that land before this one: through resources 27 (its Decided block). Their rules and estimates stay in `memory_admit.py`, and the service runs them.

**Acceptance gate:**
- Before the cut-over, the full merge gate as `docs/plans/builds-production.md` names it, green.
- `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY python3 -m unittest scripts.lint.test_memory_admit scripts.build_queue.test_admission_service scripts.build_queue.test_admission_client scripts.build_queue.test_build_queue scripts.build_queue.test_build_why scripts.build_queue.test_queue_view scripts.direct.test_verify_hold_memory scripts.direct.test_verify_token_reservation scripts.direct.test_verify_tree_cancel_reacquire scripts.direct.test_verify_checks_fail_closed scripts.direct.test_verify_lint_guard_key scripts.hooks.test_brp_launch_gate scripts.build_hold.test_build_hold scripts.build_hold.test_hold_shortcut scripts.buildlog.test_mac_idle scripts.shutdown.test_bounded_stop scripts.production.test_merge_gate_suites_admission` green.
- `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python` over each slot's changed Python files: `0 errors, 0 warnings, 0 notes`.
- `grep -n "flock" scripts/lint/memory_admit.py scripts/lint/memory_gate.sh scripts/build_queue/admission_*.py` finds nothing. `grep -n "\.waiting\|\.reservation\|admission/lock\|queue\.json"` over the Files' code files finds only the one import in `admission_service.py`.
- Live after the cut-over, at load above 100: 50 timed `admission_client.py view` calls and 50 `build_queue.py` calls, all under 1 s, p95 under 0.25 s. The summary gives the times.
- 24 h after the cut-over: no earlyoom kill caused by admission (earlyoom's journal); the median and p90 of `steps.mem_wait_s` against the 24 h before; every step row with a call id has an `admitted` row in `queue_times.jsonl`. The summary gives the numbers.
- The notice has `also touches` lines for `memory_admit.py`, `test_memory_admit.py`, `memory_gate.sh`, `pre-tool-use-brp-launch-gate.sh`, `build_hold.py`, `override_guard.py`, `override_guard.sh` and their tests (resources-unit reviewed the diff; trial-merged against `origin/builds-resources`); `invoke.sh`, `verify.sh`, `mac_idle.py` and their tests (verify-unit, against `origin/natedev-verify`); `merge_gate.py`, `stop.py` and their tests (the natedev showrunner, against `origin/natedev`); `push_catalog.py` (natedev messaging-unit, against `origin/natedev`); and `outage_alerts.py` if it changed (its owner as the builds showrunner names it).
- Change-Review line: builds wait on the queue service, and the polling admission is gone | on natedev while steps wait, type `/build:queue`, then run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/admission_client.py view` | the queue answers in under a quarter second, and each waiting step's output shows the same wait line as before.
