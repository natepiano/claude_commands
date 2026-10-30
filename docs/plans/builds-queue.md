# builds-queue

> **Status: IMPLEMENTATION PLAN — phased, direct-ready (Phase 1); Phases 2+ come from the queue service design after the user approves it.** `/build:queue` answers in under a second with one line per step; then one queue service process holds the queue, each build asks it over a socket and waits, and the polling admission goes.

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
- **Key files:** `scripts/lint/memory_admit.py` — admission decisions, the queue rows and `explain_queue`; `scripts/build_queue/build_queue.py` — the queue view and the bump CLI; `scripts/build_queue/build_why.py` — why the head waits; `scripts/build_queue/head_facts.py` — bump rows, head time, token waits; `mods/unit-board/hooks/register.tsx` — `tablePane()` and the `build:queue` command; `mods/unit-board/hooks/queue.tsx` — the pane view.
- **Test lanes:** `scripts/build_queue/test_*.py`, `scripts/lint/test_memory_admit.py`, `mods/unit-board/tests/queue.test.ts`.
- **Test:** `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY python3 -m unittest <module>` from the worktree root; the mod's test command as `docs/as-built/unit-board.md` names it.
- **Lint:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python <the slot's own Python files>`; footer must read `0 errors, 0 warnings, 0 notes`.
- **Ownership (showrunner, 2026-10-30):** queue-unit owns `scripts/build_queue/` and `commands/build/queue.md` (from report-unit), and `scripts/lint/memory_admit.py` with `scripts/lint/test_memory_admit.py` (from resources-unit). resources-unit keeps its in-flight `memory_admit.py` phases until they land (admit-head, queue order, `/showrunner:bump` priority, strict-window cap, nextest charge drop, 24B); until then each `memory_admit.py` edit is coordinated with resources-unit through berth, and resources-unit reviews the diff. `scripts/message/roster.py` is shutdown-unit's (natedev): moving `unit_labels_for` and its file-only helpers to the new `scripts/message/unit_labels.py` is approved (showrunner, 2026-10-30), and the notice carries an also-touches line. `mods/unit-board/` is natedev unit-board-unit's; the build queue parts of `register.tsx` change under an also-touches line.
- **Invariants:** admission rules live once, in `memory_admit.py`; a view prints admission's answer and repeats no budget or free arithmetic. Only the user bumps, through `/showrunner:bump`. Every answer about the queue comes in under 1 s.

## Queue service design — the user reviews it before any code

- **Who writes it:** a queue-unit design agent, started 2026-10-30 11:3x EDT, read-only; it writes `docs/plans/builds-queue-service-design.md`.
- **What it settles:** today's measured cost of polling admission; the service (what it holds, persists, how it starts and restarts); the socket protocol; admission rules kept in one place; the commands (view, the user's override, `/showrunner:bump` priority, width, release, build log rows); the rule when the service is down; performance targets; tests; and a phased migration from today's polling admission, ordered around resources-unit's in-flight phases and Phase 1.
- **Gate:** queue-unit reviews the draft, sends it to the showrunner, and the showrunner puts it before the user. No service code until the user approves.
- **After approval:** the design's migration phases become Phases 2+ of this plan through `/plan:to_phased_plan`, each with a Work Order, and go to the showrunner for approval before they run.

## Phases

### Phase 1 — /build:queue answers in under a second, one line per step, and says why each step waits  · status: todo

**Carried over** from report-unit Phase 4g (handover 2026-10-30 11:3x EDT). Report-unit's two workers started Part A at about 11:20 EDT and stopped mid-turn. Their draft edits are in `4g_partA_v2.patch` in the run's handover folder, `/tmp/claude/unit/2ac94b9b-8a9d-4a9b-b908-b4f87dd4d5d0/handover/` (`git diff --binary` against `builds-report` `e27c243c`; `git apply --3way`): `queue.md`, `queue.tsx`, `register.tsx`, `queue.d.ts`, `build_queue.py`, `head_facts.py`, `roster.py`, new `queue_card.py`, `queue_fixture.py`, `queue_lines.py`, `queue_view.py` and `scripts/message/unit_labels.py`. It is unreviewed and untested: the writer starts from it, reads all of it against the Spec and keeps only what matches. The design notes, mocks, measurements and report-unit's worker prompts are in the same folder.

**Depends on:** Part A1 depends on nothing. Part A2 depends on report-unit 4d and 4e on `origin/builds` (the draft builds on `head_facts.py` from 4e; report sends one hash after its 4c lands, about 13:00 EDT); merge `origin/builds` first. Part B also depends on resources-unit's ordered-walk checkpoint (resources Phase 30: the queue starts steps in queue order, and every bump is refused until the user's `/showrunner:bump` path lands) on `origin/builds`.

**Order of work:** resources-unit's unmerged work (its phase 25, then Phase 30) also changes `memory_admit.py`, `test_memory_admit.py`, `build_why.py`, `test_build_why.py`, `build-memory-admission.md` and `build-targets.md`, and a merge cannot land on uncommitted edits to those files. Part A, before that merge: every other file, in two steps (showrunner, 2026-10-30: start every item that does not need `head_facts.py` now). A1, now: `queue_lines.py`, the `unit_labels.py` move out of `roster.py`, the pane (`queue.tsx`, the build queue parts of `register.tsx`, `queue.d.ts`, `queue.test.ts`) and `queue.md`. A2, after the unit director merges `origin/builds` with report-unit 4d and 4e: `head_facts.py`, `queue_view.py`, `queue_card.py`, `queue_fixture.py`, `build_queue.py` and the golden files. In Part A `build_queue.py` keeps `locked_queue_read()` and the existing `LineWaiter`, and `queue_lines.py` gets its own cell width. Part B, after the unit director merges `origin/builds`: Spec 1–3, `build_why.py` takes its cell width from `queue_lines.py` (one copy), the switch to `queue_read()`, the docs for those files and the live timing.

**Bridge to the service:** the `queue.json` snapshot (Spec 1) is how the view answers in under 1 s while polling admission stays. The service design says whether the service's view command replaces it; the view, card and renderer (Spec 4–11) read whichever source the design picks.

#### Work Order

**Goal:** `/build:queue` prints one line per step at every width, says once why the steps wait (with each check for #1 as ✓/✗ and its numbers), marks each moved-up step with who moved it and from where, gives a `starts` estimate, and `/build:queue <position|session|folder>` prints one step's card. The view, build-why and the card each answer in under 1 s.

**Spec:**

User requirements (2026-10-30, verbatim): "build queue is unusable for me ... i can't parse this ... it is acceptable to be wider ... be more creative ... it is TOO slow"; "i need instantaneous answers on builds ALWAYS ... i cannot get clarity"; "i need to get sub second responses to any questions like these"; "only I can bump". Bump priority comes only from the user's `/showrunner:bump`, scoped to that target, for the time the user names; resources-unit builds that enforcement, not this phase.

Measured (natedev, 2026-10-30 10:30–11:00 EDT, load 130–184 on 32 cores): `build_queue.py` takes 3.3–6.1 s. The full roster build takes 3.9 s for about 28 labels; `roster.unit_labels_for` gives the same labels in 0.02–0.12 s. The shared admission lock wait is 0.13–7.24 s (median 2.07 s): about 6 `check` passes per second hold `LOCK_EX` 77% of the time. Imports cost 0.22–0.54 s. The pane is about 89 columns; the waiting table needs about 256, so `queue.tsx` prints about 370 `Label: value` lines for 32 steps.

1. **Queue snapshot (one lock-free read).** `memory_admit.py` edits start only after resources-unit's ordered-walk checkpoint is on origin/builds; resources-unit reviews the memory_admit.py diff.
   - Split `read_queue_pass` into `gather_queue_inputs(directory, processes, now, memory: Mapping[Path, StepMemory] = {}) -> QueueInputs` (every file and `/proc` read it does today: waiter rows with their file names, reservation rows with their measured memory and resolved identity, sccache memory, the meminfo reading or unavailable, pressure after recovery, `now`) and a pure `queue_read_from(inputs) -> QueueRead` (no file read). `read_queue_pass` calls both, so admission, the locked read and the snapshot reader run one decision path. `memory` lets a caller pass readings it already took; a missing path is read.
   - `write_queue_snapshot(directory, inputs)` writes `admission/queue.json` (`{"v": 1, "written_at": <epoch>, "inputs": ...}`) through `queue.json.tmp` and `os.replace`, no fsync (the file is a cache; an unreadable file reads as absent). The serial form and its parser live in `memory_admit.py` only; writer and reader import the same code.
   - One helper calls it at the end of every `LOCK_EX` section: `check`, `bump_waiter`, `release`, `set_width`, and any exclusive section the ordered-walk checkpoint adds (its `give-back`). The write runs after the section's own changes, on success and on an exception path. `check` passes the process table and the memory readings it already took. A failed write (OSError, ValueError) never changes the section's result or exit code; the old file stays and goes stale.
   - `queue_read() -> QueueRead`: parse `queue.json`; when `0 <= time.time() - written_at <= SNAPSHOT_MAX_AGE_SECONDS` (10) and the version and shape are valid, return `queue_read_from(inputs)`; else return `locked_queue_read()`. With waiters, their polls rewrite the file every few seconds; with no waiters nobody polls, so the lock is free. The reading's `now` is the snapshot's `now`; the view shows it as the "as of" time.
   - Moving the `/proc` scan out of the lock and fewer `check` passes stay with resources-unit (not in this phase).
2. **Per-step checks from admission.** `read_queue_pass` already computes each waiter's own `queue_decision`. Keep its numbers: `LineWaiter` gains `decision: AdmissionDecision`, `budget: BudgetCheck` and `free: FreeCheck`, built by the one function that builds the head's `BudgetCheck` and `FreeCheck` today (no second construction). `fits_now` stays.
3. **One selector matcher.** `memory_admit.match_waiters(selector: str, view: QueueView) -> list[int]` holds the match rule that `bump_waiter` uses today (position, session id, session name, folder, folder name). `bump_waiter` calls it under the lock; the card calls it on the snapshot reading. `build_queue.bump_selector` (shown names, report-unit 4a) stays the front of both.
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
- `scripts/lint/memory_admit.py` — `gather_queue_inputs`, `queue_read_from`, `write_queue_snapshot` and its calls in every `LOCK_EX` section, `queue_read`, `SNAPSHOT_MAX_AGE_SECONDS`, `LineWaiter` checks, `match_waiters` (owner resources-unit; it reviews this diff)
- `scripts/lint/test_memory_admit.py` — snapshot and matcher tests (owner resources-unit)
- `scripts/build_queue/build_queue.py` — the read, labels, CLI words, `--columns`, `--json`
- `scripts/build_queue/queue_lines.py` — new: spans, cell widths, cut and wrap, plain and JSON forms
- `scripts/build_queue/queue_view.py` — new: Mock A
- `scripts/build_queue/queue_card.py` — new: Mock B
- `scripts/build_queue/head_facts.py` — `BumpRecord` scan to the oldest waiter, per-row moves, pace reader
- `scripts/build_queue/build_why.py` — `queue_read()`, cell width from `queue_lines`
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
  - Snapshot: a written file round-trips to the same `QueueRead` as the locked read; age 10 s uses it, 10.1 s and a future time take the locked read; wrong `v`, bad JSON and a missing file take the locked read; each of `check`, `bump_waiter`, `release`, `set_width` rewrites it; a failed write leaves the result and the old file; a reader never sees a partial file (temp + replace).
  - `match_waiters` gives the same matches `bump_waiter` used before; `LineWaiter` checks equal the head's construction for the same row.
  - Timing: a fake `roster.roster` that sleeps 5 s and a counting `unit_labels_for`: the view and the card finish under 1 s, `roster.roster` is never called, `unit_labels_for` is called once per run with every shown id.
  - Pane: paints spans in order with their tones; no record lines at any width; a width change asks one forced refresh; `/build:queue 4` passes the selector; invalid JSON shows `Build queue could not be read.`

**State:** `admission/queue.json` (new; rewritten under the admission lock at the end of every exclusive section; a cache that readers ignore when older than 10 s).

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
- Live on natedev, steps waiting, from the worktree's scripts: three runs each of `build_queue.py`, `build_queue.py --json --columns 89`, `build_queue.py 4` and `build_why.py`, every run under 1.0 s wall; the summary lists the 12 times and whether each read the snapshot. No screenshot.
- Live: the snapshot write adds at most 10 ms median to an exclusive hold over at least 200 `check` passes; the summary gives the median and the max.
- The notice has `also touches` lines for `memory_admit.py` and `test_memory_admit.py` (resources-unit, against `origin/builds-resources`; resources-unit reviewed the diff) `register.tsx` (natedev unit-board-unit, against `origin/natedev`) and `scripts/message/roster.py` (shutdown-unit, against its branch tip).
- Change-Review line: /build:queue shows one line per step and answers one step at once | on natedev while steps wait, run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_queue.py`, then the same with `4` | one line per step with ↑ marks and a `#1 waits on` block, and a card whose first line answers `Next?`, each in under a second.
