# builds-report

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `/build:queue` opens a pane from the unit-board mod, with no model turn, as `/accounts-board` does, and says at once why the head step waits; the build log index stops rewriting its source logs; app launches reach the build log again with their memory wait.

> **As-built disposition: amend** — `docs/as-built/unit-board.md` (the shared table pane and `/build:queue`); `docs/as-built/build-memory-admission.md` (Queue API); `docs/as-built/buildlog-memory-stalls.md` (index reads old field names)

> **Production: builds** — unit `report-unit`; production doc `docs/plans/builds-production.md`
## Source

2026-10-29 21:00 EDT (the user, to report-unit)

build queue - i need you to make it work  like /accounts-board - so that it can load quickly without going through a skill - if it can share code to do this claude code modifiction that would be fine

2026-10-29 20:58 EDT (the showrunner)

the user-approved rerun sweep (docs/plans/sdlc-rerun-sweep.md, main 4dfc4dce) has 1 row for report-unit. Plan it as a small phase, with a test that stops at the named line and then reruns.

The row: `scripts/buildlog/index.py:689` (Q3). Interrupt the source-log rewrite after it writes only part of the new bytes. The rerun keeps malformed lines during migration and skips them during indexing; the original records have been overwritten and cannot be restored from this file.

2026-10-29 21:04 EDT (the user, after a `/build:queue` run showed 33 steps waiting 74 min behind a head step while 27.6 GiB was free)

first run a /build:queue - there is very little memory when i'm typing this that is being taken - yet the first one in the queue is not running

this question comes up so often - i need an instantaneous way to answer it

2026-10-29 21:05 EDT (the showrunner)

the build log has no brp-launch rows after 2026-10-15T18:59Z (411 rows from 09-09 to 10-15), and none of those rows carries mem_wait_s. Find why app launches stopped being recorded, and restore the rows with their memory wait. If the cause is in resources' launch hook, send me the cause and I'll route it. I need those numbers to judge whether launches lose time in the memory queue.

2026-10-29 22:0x EDT (the user, relayed by the showrunner, who named it `build-why`)

i need a command that i can run at the cli - that tells me exactly what is going on - clearly with the running and the first one waiting (what it needs) and how many arein line - different than the build:queue - something that runs fast and tells me why the current one is waiting showing the me analysis that we have done so far

including the details to make it clear

## Delegation Context

- **Project:** `~/.claude` — the build log, the build queue view and the unit-board Claude Code mod.
- **Project started:** 2026-10-29T11:45:32-04:00
- **Stack:** Python 3.13 (basedpyright strict, unittest); TypeScript mod under `mods/unit-board/` (its own tests in `mods/unit-board/tests/`).
- **Layout:** `scripts/buildlog/` (record, index, launches, report); `scripts/build_queue/build_queue.py`; `scripts/lint/memory_admit.py` (read only: resources-unit's); `mods/unit-board/hooks/` and `mods/unit-board/tests/`; `commands/build/queue.md`; `scripts/hana_shot/hana_shot.py` (natedev screenshot-unit's).
- **Key files:** `scripts/build_queue/build_queue.py` — the queue tables; `mods/unit-board/hooks/register.tsx` — mod commands, panes, timers; `mods/unit-board/hooks/accounts.tsx` — the accounts pane view; `scripts/buildlog/index.py` — the SQLite index over the append-only `.jsonl` logs; `scripts/buildlog/store.py` — where records live and how a line is appended; `scripts/buildlog/launches.py` — launch rows from transcripts.
- **Test lanes:** `scripts/buildlog/test_*.py`, `scripts/build_queue/test_build_queue.py`, `mods/unit-board/tests/`.
- **Test:** `python3 -m unittest <module>` from the worktree root for Python; the mod's test command as its as-built (`docs/as-built/unit-board.md`) names it.
- **Lint:** `basedpyright <the slot's own Python files>`; footer must read `0 errors, 0 warnings, 0 notes`.
- **Ownership:** `mods/unit-board/` belongs to natedev's unit-board-unit; the user asked report-unit for this change and allowed shared code (21:00 EDT). The showrunner approved Phases 1 and 2 side by side and told natedev. `scripts/lint/memory_admit.py` is resources-unit's: no edit in Phases 2 and 4 (showrunner, 21:10 EDT); Phase 1 adds only the typed bump refusal `UnmatchedWaiter(ValueError)` (unit director's call 22:3x: the refusal is decided there, so its type lives there); Phase 3 adds its read-only explain function and the waiter's need-source fields (showrunner, 22:0x EDT), and its notice names resources as also-touches, tested against origin/builds-resources. `scripts/hana_shot/hana_shot.py` is screenshot-unit's: edit it only through cargo-berth, and the checkpoint notice names it.
- **Invariants:** build-log `.jsonl` files are appended and never edited (`store.py` docstring). Shown names are separate short names; only copy folders join showrunner and unit.

## Phases

### Phase 1 — /build:queue opens a mod pane and says at once why the head waits  · status: done

#### As-built

- `build_queue.py --json` prints one `QueueSnapshot` object: running and waiting rows with their headers, the head line `{"text", "tone"}`, the totals and the roster problems. Each cell is formatted once, in the user's zone. The text tables and the JSON come from the same row values.
- `head_line()` gives one line above the Waiting table, in the text output and in the pane. It reads the decision through `memory_admit`'s queue view and keeps no copy of its rules. A budget hold names each running step that holds budget, with its charge and its measured use. A free-memory hold gives free memory against the step's need. A pressure hold gives the pressure reason. When the head has waited 15 min or more and free memory covers its need, the line starts `Stalled:` with tone `error`; else it starts `Head:` with tone `default`.
- The unit-board mod answers `command.run` for `build:queue` with no model turn. It opens pane `build-queue` (title `Build queue`), runs `build_queue.py --json`, refreshes every 10 s, and closes on `close`, Escape or the close mark. A failed run shows `Build queue could not be read.`, never process output. `bump <selector>` runs `build_queue.py bump <selector>` and replies with its result line. The pane refresh after a bump is outside the bump's error catch, so a refresh failure never replaces the bump reply.
- `tablePane()` in `register.tsx` is the one table-pane lifecycle: open, loading, one run per tick, busy guard, 10 s timer, restart on reload, close, fixed failure text. `/accounts-board` and `/build:queue` both use it.
- A bump selector refusal is a type. `memory_admit.bump_waiter` raises `UnmatchedWaiter(ValueError)` for an unknown or ambiguous selector, and `build_queue.py`'s shown-name match raises the same type for an ambiguous name. `build_queue.py` catches only `UnmatchedWaiter`: it prints `build queue: <text>` on stdout, with control characters escaped, and exits 3. The mod forwards only that exit 3 reply.
- `commands/build/queue.md` still runs the script, so a session without the mod gets the tables. It names `/reload-plugins` for a session that started before the mod change.

**Files:**
- `scripts/build_queue/build_queue.py` — `--json`, `head_line()`, the bump CLI and its exit 3 refusal
- `scripts/lint/memory_admit.py` — `UnmatchedWaiter(ValueError)`, raised at `bump_waiter`'s match check
- `mods/unit-board/hooks/register.tsx` — `tablePane()` helper; the `build:queue` command
- `mods/unit-board/hooks/queue.tsx` — the build queue pane view
- `mods/unit-board/types/queue.d.ts` — the `QueueSnapshot` JSON shape
- `commands/build/queue.md` — fallback body and the `/reload-plugins` line
- `docs/as-built/unit-board.md` — the shared pane and `/build:queue`
- `scripts/build_queue/test_build_queue.py`, `mods/unit-board/tests/queue.test.ts` — tests

**Binds later work:** the mod pane reads `--json`, so the head line's JSON field keeps the `{"text", "tone"}` shape when build-why takes over its text. A selector refusal is `memory_admit.UnmatchedWaiter`, printed on stdout with exit 3; the mod forwards only that.

**Gotchas:** only `UnmatchedWaiter` is a refusal. Every other bump error, a `ValueError` from the locked reread included, prints on stderr and exits 2. A refusal found by exit code or text prefix alone lets operational errors through to the pane.

**Ruled out:** a refusal channel by exit code or message prefix without a type — it leaks operational errors.

### Phase 2 — The index reads old field names and never rewrites a source log  · status: done

#### As-built

- The build-log index never writes a `.jsonl` source log. `migrate_worker_fields` is gone.
- `read_file` maps old field names at index time. For a `step` or `call` record, each `FORMER_FIELDS` pair (`seat`→`worker`, `delegate_session`→`unit_session`) fills the new name from the old one; a record with both keeps the new value. An old-name record and a new-name record give the same index row.
- A stop at any line of an index update leaves every source byte unchanged, and a rerun indexes each record once. This closes the rerun sweep row for `scripts/buildlog/index.py:689`.

**Files:**
- `scripts/buildlog/index.py` — `FORMER_FIELDS` read-side mapping in `read_file`; no source rewrite
- `scripts/buildlog/test_index_rerun.py` — stop-and-rerun test: source bytes unchanged, new field names, a second rerun adds nothing
- `scripts/buildlog/test_index.py` — `test_first_index_read_maps_worker_fields_and_leaves_source_unchanged` checks unchanged source bytes
- `docs/as-built/buildlog-memory-stalls.md` — the index reads old field names

**Binds later work:** the index never rewrites a source log.

**Gotchas:** the live logs still hold old-name fields (about 70 at closeout). No code rewrites them; only the index read maps them, so a reader that opens the `.jsonl` files directly must map `FORMER_FIELDS` too.

### Phase 3 — build-why says at once why the first waiting step waits, from admission's own numbers  · status: todo

#### Work Order

**Goal:** `build-why` at the command line prints in under 1 s what runs, what waits first and why, with each check's arithmetic, from one read-only explain record in `memory_admit.py` that admission's own functions fill.

**Spec:**
- One decision point. Add one read-only function to `scripts/lint/memory_admit.py`, `explain_queue() -> QueueExplanation`, that takes the shared admission lock as `queue_view()` (memory_admit.py:1525) does. It fills every number with admission's own functions on the same pass: `reservation_memory`/`step_memory` (or `reservation_lifetime` once resources' Phase 14 lands: never a copy of the "pid start time matches" check, so an orphaned step the queue still charges stays in the list), `build_charge`, `growth_headroom`, `decide`, `head_priority`, `head_window`, `queue_decision`, `read_meminfo`, `recovery_pressure`. `read_queue` (memory_admit.py:1465) and `explain_queue` build from one shared internal pass, so the two cannot disagree; `queue_view()` returns the same `QueueView` as now. No second calculation of any rule. Every number in the record is a typed field in bytes or epoch seconds; text is made only by the caller.
- `QueueExplanation` (frozen dataclasses, one variant per case, per `~/.claude/docs/type_design.md`):
  - host: available, total, earlyoom reserve (5% of total, as `decide`), build budget, pressure (`NoPressureHold` or `MemoryPressure` with its recovery spacing time), reading unavailable as its own variant.
  - running: per step the identity as `RunningStep` has it, admitted time, promise (`need`), measured bytes, charged (`build_charge`), and a `ChargeBasis` variant naming the `build_charge` term that sets the charge: promise with no reading yet; ramp promise (inside `RAMP_SECONDS` of admission); peak so far (observed peak, with the time of that peak where the tree records it); peak decayed (the 25-min decay and the old-sampler rule from resources' Phase 12 patches B and C on origin/builds, with their times); peak file unreadable (promise kept); using now. One variant per rule the merged tree has; a rule resources adds later (Phase 15 test-phase peak) adds its variant when it lands; whoever lands second resolves it.
  - totals: charged and using now over all running steps, and the promise not yet used (`growth_headroom` sum).
  - head: no waiter, or the first waiter in `waiter_order` with its identity, wait start, need, need source (below), and its decision from `queue_decision` on the same inputs; the budget check (charged, need, head need, budget, pass or fail, and the charge it starts at: budget − need − head need); the free check (free, need, earlyoom reserve, promise not yet used, head need, threshold, pass or fail); the order rule from `head_window` (`StrictUntil` or `BackfillUntil` with its deadline), whether `head_priority` gives the head priority, and its bump (`bump_order` < 0 or `holds_younger`, with `priority_since`).
  - line: the waiters behind the head; for each, whether `decide` with no head need admits it under both checks now; and their count, largest and smallest need, oldest wait start.
- The waiter row records where its need came from, at estimate time, in `check` (memory_admit.py:1227-1236). New `Waiter` fields: `need_source` (`ExpectedPeak.source`: measured / buildlog / pooled / fallback), `need_runs` (`ExpectedPeak.count`), `test_threads` (the width `test_threads(argv)` reads, or null). The window is `HISTORY_DAYS`; test scope, package and filters are on the row already. A row without `need_source` (written before this change) reads as "source not recorded". `explain_queue` never opens the build-log index or any SQLite file.
- `scripts/build_queue/build_why.py` (new, run as `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_why.py`; natedev adds the `build-why` PATH wrapper after main): prints `explain_queue()` in plain words, a unit on every number (GiB with one decimal, minutes, HH:MM in the user's zone as `build_queue.py` does), with Showrunner and Unit names from `build_queue.py`'s roster labels. Sections, in order:
  1. Header: time; free of total memory; build charges of the 21.0 GiB budget; the pressure hold or recovery spacing, if any; load (`/proc/loadavg`, the 1-min value of the core count).
  2. Running: one line per step: Showrunner, Unit, session, step, age, charged, using now, and why it is charged that much (the `ChargeBasis` in words: `compiling, peak so far 10.8 GiB`, `peak decayed at 21:40`, `old-sampler rule: peak drops 25 min after its last new peak`, `promise: no reading yet`). Then `Total: charged X GiB, using Y GiB`.
  3. First in line: Showrunner, Unit, session, step, wait in minutes, need, and where the need comes from (for example `measured process-memory p90 of 7 runs, last 14 days, filtered tests (pkg, 2 filters), width 8`, `build-log peak × 0.65 of 12 runs`, `pooled over all repos, 31 runs`, `fallback: fewer than 5 runs`, `source not recorded`).
  4. The two checks, each with its arithmetic and result: `Build budget: charged 18.1 + need 11.8 = 29.9 GiB > 21.0 GiB: blocked; starts when charged falls to 9.2 GiB` and `Free memory: free 16.3 GiB < need 11.8 + earlyoom 3.0 + promise not yet used 5.5 = 20.3 GiB: blocked`. A head need term appears only when it is non-zero. Pressure, when it holds, is its own line.
  5. Order rule: `strict until HH:MM: younger steps wait` or `backfill until HH:MM: younger steps that fit may start`, and any bump (`bumped by position at HH:MM`).
  6. The line: `N waiting behind it; M would fit now under both checks; needs from A to B GiB; oldest wait W min`.
  7. Last line: `Waiting because: <the failing checks, the order rule if it binds>. Frees when: <the fewest running steps, largest charge first, whose end brings the failing check to pass, by how much>` — for the budget, the drop needed is charged + need + head need − budget; for free memory, threshold − free. With no waiter: `Nothing waits.`
  A read failure prints one fixed line, `build-why: the admission state could not be read.`, and exits 2; never a traceback.
- `build_queue.py`'s head line (Phase 1) prints from the same `QueueExplanation` fields, never its own copy of the budget arithmetic. Write this change only after Phase 1's checkpoint commit (both phases edit `build_queue.py`).
- Fast: under 1 s on natedev with tonight's queue size: reads only the admission state files, `/proc`, `/proc/meminfo` and `/proc/loadavg`.

**Files:**
- `scripts/lint/memory_admit.py` — `explain_queue`, `QueueExplanation` and its variants, the shared pass with `read_queue`, the waiter's need-source fields (owner resources-unit; showrunner allowed this change 2026-10-29 22:0x)
- `scripts/build_queue/build_why.py` — new: the command
- `scripts/build_queue/build_queue.py` — head line from `QueueExplanation` (after Phase 1's checkpoint)
- `scripts/build_queue/test_build_why.py` — new: fixture from tonight's 21:51 case; output and verdict checks
- `scripts/lint/test_memory_admit.py` — new tests only: waiter need-source fields; `explain_queue` equals `read_queue`'s decision on the same files; no SQLite opened
- `docs/as-built/build-memory-admission.md` — the explain record and the waiter's need-source fields
- `commands/build/queue.md` — one line naming `build-why`

**Workers:** 1 writer + 1 tester.
- `impl2` — `memory_admit.py`, `build_why.py`, `build_queue.py`, the as-built and `queue.md`; hub: `memory_admit.py` (both test files import its types)
- `test2` — `test_build_why.py` and the new `test_memory_admit.py` tests, from this Spec. Fixture: tonight's 21:51 case as admission state files plus fake `/proc` and meminfo (as `test_build_queue.py` and `test_memory_admit.py` build them): running widget, demo, organon and frame-cost; inspectors at the head; numbers so the head shows charged 18.1 GiB, need 11.8 GiB, free 16.3 GiB, earlyoom 3.0 GiB, promise not yet used 5.5 GiB. Checks: `build_why.py`'s verdict for the head equals `memory_admit.read_queue`'s head reason and `queue_decision` state on the same files; each section's numbers; an old waiter row without `need_source`; `sqlite3.connect` patched to fail does not change the output; a read failure prints the fixed line and exits 2.

**State:** new fields in each `*.waiting` row (written by `check`; readers ignore unknown fields)

**Depends on:** Phase 1 for the `build_queue.py` head line only; the rest runs beside it.

**Constraints from prior phases:** Phase 1: `build_queue.py --json` and the head line exist; the mod pane reads `--json`, so the head line's JSON field keeps its shape. Merge origin/builds first: it carries resources' Phase 12 patches B and C (peak decay) in `memory_admit.py`. Resources' Phase 14 (in review 22:0x) adds `reservation_lifetime(row, owner_live)` and keeps every signature; Phase 15 adds a test-phase peak estimate source. Also-touches resources: trial-merge and test against origin/builds-resources before the notice.

**Acceptance gate:** `python3 -m unittest scripts.build_queue.test_build_why scripts.build_queue.test_build_queue scripts.lint.test_memory_admit` green (prefix `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY`); basedpyright 0/0/0 on the changed Python files. Live: `build_why.py` on natedev finishes under 1 s, and its head verdict matches `/build:queue` run at the same time.

### Phase 4 — App launches reach the build log again, with their memory wait  · status: todo

#### Work Order

**Goal:** the build log has a launch row for every app launch after 2026-10-15T18:59Z, each with `mem_wait_s`, and `/build:report` shows them.

**Spec:**
- Known: `scripts/buildlog/launches.py` collects launches only from `mcp__brp__brp_launch` tool results in Claude transcripts. Since 10-16 only 12 transcripts hold such a call; launches go through `/hana_shot` (`scripts/hana_shot/hana_shot.py`) and `verify.sh build`. No row ever had `mem_wait_s`.
- First confirm the cause against the live tree and log. Then record each launch where it happens, as one append-only row through the build-log recorder, carrying the admission wait from the memory queue's own record. Both the `hana_shot.py` path and the `verify.sh build` path write it. Backfill from 10-15 onward where stored records allow it, and report the count.
- A cause in resources' launch hook goes to the showrunner with evidence; it is not fixed here.
- screenshot-unit's atomic launch record in `hana_shot.py` (near line 2078) is on main 9b6e2102. Merge origin/main through origin/builds first. The build-log row is built on top of that record: when the record holds what the row needs, read it and add no second write. The merge waits for the end of tonight's no-merge hold (the showrunner names its start and end). The notice names screenshot-unit (natedev) as also-touches for `hana_shot.py`, tested against origin/main.

**Files:**
- `scripts/buildlog/launches.py` — launch rows from the launch paths, backfill
- `scripts/buildlog/record.py` — the launch row with `mem_wait_s`
- `scripts/buildlog/report.py` — count and show the rows
- `scripts/hana_shot/hana_shot.py` — one recorder call (berth; owner screenshot-unit)
- `scripts/buildlog/test_launches.py` — rows per path, backfill rerun adds no duplicate

**Workers:** 1 writer + 1 tester.
- `impl2` — `launches.py`, `record.py`, `report.py`, `hana_shot.py`
- `test2` — `test_launches.py`

**State:** build-log rows; the backfill's launches state

**Depends on:** Phase 2 (both edit `scripts/buildlog/`; Phase 3 reads index rows).

**Constraints from prior phases:** Phase 2: the index never rewrites a source log.

**Acceptance gate:** the buildlog tests green; basedpyright 0/0/0. Live: after one `/hana_shot` on this unit's port, the build log has a new launch row with `mem_wait_s`, and `/build:report` counts it.

### Phase 5 — A bump by shown name moves only the step it names  · status: todo

#### Work Order

**Goal:** `build_queue.py bump <shown name>` moves the waiting step that name matched, or refuses; it never moves another step because the queue changed between reads.

**Spec:**
- Defect (Astra review of phase 1 F005, 2026-10-29; pre-existing since e6baef45): `bump_selector` (`scripts/build_queue/build_queue.py:248`) turns a roster shown name into a position from its first queue read; `memory_admit.bump_waiter` applies that position to its locked reread. With waiters `[A, B, C]`, selecting `B` gives `"2"`; if `B` leaves first, position 2 moves `C`.
- Fix: resolve the shown name to a stable waiter identity (pid and start time from the waiter row) and have `bump_waiter` accept that identity and check it under the lock; a waiter that has left gives the typed `UnmatchedWaiter` refusal naming the original selector. Session id alone is not enough: several waiters can share one.
- Tests: a waiter removed between the two reads, and a reorder between them; each refuses or moves the named step only.

**Files:**
- `scripts/build_queue/build_queue.py` — `bump_selector` returns a waiter identity
- `scripts/lint/memory_admit.py` — `bump_waiter` takes and checks the identity under the lock (also-touches resources)
- `scripts/build_queue/test_build_queue.py` — removal and reorder cases

**Workers:** 1 writer + 1 tester.
- `impl` — `build_queue.py`, `memory_admit.py`
- `test` — `test_build_queue.py`

**State:** waiter rows (`bump_order`, `holds_younger`, `priority_since` written by a bump)

**Depends on:** Phase 1 (the typed `UnmatchedWaiter` refusal) and Phase 3 (both edit `memory_admit.py`).

**Constraints from prior phases:** Phase 1: a selector refusal is `memory_admit.UnmatchedWaiter`, printed on stdout with exit 3; the mod forwards only that.

**Acceptance gate:** `python3 -m unittest scripts.build_queue.test_build_queue scripts.lint.test_memory_admit` green (prefix `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY`); basedpyright 0/0/0 on the changed files.
