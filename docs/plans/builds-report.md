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

## Delegation Context

- **Project:** `~/.claude` — the build log, the build queue view and the unit-board Claude Code mod.
- **Project started:** 2026-10-29T11:45:32-04:00
- **Stack:** Python 3.13 (basedpyright strict, unittest); TypeScript mod under `mods/unit-board/` (its own tests in `mods/unit-board/tests/`).
- **Layout:** `scripts/buildlog/` (record, index, launches, report); `scripts/build_queue/build_queue.py`; `scripts/lint/memory_admit.py` (read only: resources-unit's); `mods/unit-board/hooks/` and `mods/unit-board/tests/`; `commands/build/queue.md`; `scripts/hana_shot/hana_shot.py` (natedev screenshot-unit's).
- **Key files:** `scripts/build_queue/build_queue.py` — the queue tables; `mods/unit-board/hooks/register.tsx` — mod commands, panes, timers; `mods/unit-board/hooks/accounts.tsx` — the accounts pane view; `scripts/buildlog/index.py` — the SQLite index over the append-only `.jsonl` logs; `scripts/buildlog/store.py` — where records live and how a line is appended; `scripts/buildlog/launches.py` — launch rows from transcripts.
- **Test lanes:** `scripts/buildlog/test_*.py`, `scripts/build_queue/test_build_queue.py`, `mods/unit-board/tests/`.
- **Test:** `python3 -m unittest <module>` from the worktree root for Python; the mod's test command as its as-built (`docs/as-built/unit-board.md`) names it.
- **Lint:** `basedpyright <the slot's own Python files>`; footer must read `0 errors, 0 warnings, 0 notes`.
- **Ownership:** `mods/unit-board/` belongs to natedev's unit-board-unit; the user asked report-unit for this change and allowed shared code (21:00 EDT). The showrunner approved Phases 1 and 2 side by side and told natedev. `scripts/lint/memory_admit.py` is resources-unit's: never edit it (showrunner, 21:10 EDT). `scripts/hana_shot/hana_shot.py` is screenshot-unit's: edit it only through cargo-berth, and the checkpoint notice names it.
- **Invariants:** build-log `.jsonl` files are appended and never edited (`store.py` docstring). Shown names are separate short names; only copy folders join showrunner and unit.

## Phases

### Phase 1 — /build:queue opens a mod pane and says at once why the head waits  · status: todo

#### Work Order

**Goal:** `/build:queue` opens pane `build-queue` (title `Build queue`) at once with no model turn, like `/accounts-board`, and one head line says what holds the first waiting step.

**Spec:**
- `build_queue.py` gains `--json`: one object with the running rows, the waiting rows, the head line, the totals and the roster problems. Each cell is already formatted in the user's zone, as the text tables show it. The text output keeps its form; text and JSON come from the same row values.
- Head line, above the Waiting table, in both the text output and the pane. A budget hold names each running step that holds the budget, with its charge and its measured use, for example `widget-enhancements-title nextest: charged 10.8 GiB, using 0.5 GiB`. A free-memory hold gives free memory against the need, and a pressure hold gives the pressure reason. When the head has waited 15 min or more while free memory covers its need, the line starts `Stalled:` in the error tone. The line comes from the decision `memory_admit.py` already makes, read through its existing queue view, never a second copy of its rules.
- The mod answers `command.run` on `{ command: 'build:queue' }`, as it answers `showrunner:unit_board`, and never calls `next`. Open: store `loading`, run `[~/.claude/scripts/lib/py, ~/.claude/scripts/build_queue/build_queue.py, "--json"]`, reply `Build queue opened.`. Refresh every 10 s while open. `close`, Escape or the close mark closes it (`Build queue closed.`). `bump <selector>` runs `build_queue.py bump <selector>`, replies with its result line and refreshes an open pane. It never parses the text table. A failed run shows `Build queue could not be read.`, never process output.
- Shared code: the accounts pane lifecycle in `hooks/register.tsx` (open, loading, one run per tick, busy guard, 10 s timer, reload restart from `$.ui.panes()`, close, fixed failure reason) becomes one table-pane helper. `/accounts-board` and `/build:queue` both use it; `/accounts-board` behaves exactly as before.
- `commands/build/queue.md` keeps its frontmatter; its body still runs the script, so a session without the mod still gets the tables, and names `/reload-plugins` for a session started before the mod change.
- Assertion-Change: `scripts/build_queue/test_build_queue.py` — `test_budget_reason_names_charged_builds_and_the_limit`, `test_memory_reason_names_free_memory_and_earlyoom_reserve` and `test_unavailable_reading_does_not_claim_zero_free_memory` select only table rows (lines that start with `| `), because the head line now holds the same reason text. Each assertion string stays the same.

**Files:**
- `scripts/build_queue/build_queue.py` — `--json`, head line
- `scripts/build_queue/test_build_queue.py` — JSON equals text cells; head line per hold kind; `Stalled:` at 15 min
- `mods/unit-board/hooks/register.tsx` — table-pane helper; `build:queue` command
- `mods/unit-board/hooks/queue.tsx` — new: the build queue pane view
- `mods/unit-board/types/queue.d.ts` — new: the JSON shape
- `mods/unit-board/tests/queue.test.ts` — new: pane tests
- `commands/build/queue.md` — fallback body and `/reload-plugins` line
- `docs/as-built/unit-board.md` — the shared pane and `/build:queue`

**Workers:** 1 writer + 1 tester.
- `impl` — `build_queue.py`, `register.tsx`, `queue.tsx`, `queue.d.ts`, `commands/build/queue.md`, `docs/as-built/unit-board.md`
- `test` — `test_build_queue.py`, `mods/unit-board/tests/queue.test.ts`

**State:** none

**Depends on:** none

**Constraints from prior phases:** UnitLabels gives separate short Showrunner and Unit names (roster.py `_unit_labels`); keep those columns.

**Acceptance gate:** `python3 -m unittest scripts.build_queue.test_build_queue` green; the mod test suite green with `accounts.test.ts` and `register.test.ts` unchanged; basedpyright 0/0/0 on the changed Python files. Live: after `/reload-plugins`, `/build:queue` opens the pane with no model turn, its rows match `python3 ~/.claude/scripts/build_queue/build_queue.py` run at the same time, and the head line names the step that holds the budget when one does.

### Phase 2 — The index reads old field names and never rewrites a source log  · status: done

#### Work Order

**Goal:** a stop at any line of an index update cannot change or lose a build-log record.

**Spec:**
- Remove `migrate_worker_fields` (`scripts/buildlog/index.py:663-692`) and its call in `read_file`. While indexing a `step` or `call` record, read the old names as the new ones from `FORMER_FIELDS` (`seat`→`worker`, `delegate_session`→`unit_session`); a record with both keeps the new value.
- The rerun sweep row (`docs/plans/sdlc-rerun-sweep.md` on main 4dfc4dce, `scripts/buildlog/index.py:689`, Q3) is closed by this: no index code writes a `.jsonl` file.
- Assertion-Change: `scripts/buildlog/test_index.py` `test_first_index_read_migrates_worker_fields_and_preserves_incomplete_appends` becomes `test_first_index_read_maps_worker_fields_and_leaves_source_unchanged`: its two `assertNotIn` checks for `"seat"` and `"delegate_session"` in the source become one `assertEqual` of the source bytes before and after the update. The row, partial-append and second-update checks stay.

**Files:**
- `scripts/buildlog/index.py` — drop the rewrite; read-side rename
- `scripts/buildlog/test_index_rerun.py` — new: stop-and-rerun test
- `scripts/buildlog/test_index.py` — the old rewrite test now checks unchanged source bytes
- `docs/as-built/buildlog-memory-stalls.md` — one line: the index reads old field names

**Workers:** 1 writer + 1 tester.
- `impl2` — `index.py`, the as-built line
- `test2` — `test_index_rerun.py`, `test_index.py`

**State:** index rows (`index.sqlite`)

**Depends on:** none

**Constraints from prior phases:** none

**Acceptance gate:** the new test stops the update where the rewrite ran (an exception raised after the first record is indexed), reruns it, and checks that every record is in the index under its new field names, the source bytes are unchanged, and a second rerun adds nothing; an old-name and a new-name record give the same row. `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py' -t .` green from the worktree root (or the module list it runs in the merge gate); basedpyright 0/0/0 on the changed files.

### Phase 3 — App launches reach the build log again, with their memory wait  · status: todo

#### Work Order

**Goal:** the build log has a launch row for every app launch after 2026-10-15T18:59Z, each with `mem_wait_s`, and `/build:report` shows them.

**Spec:**
- Known: `scripts/buildlog/launches.py` collects launches only from `mcp__brp__brp_launch` tool results in Claude transcripts. Since 10-16 only 12 transcripts hold such a call; launches go through `/hana_shot` (`scripts/hana_shot/hana_shot.py`) and `verify.sh build`. No row ever had `mem_wait_s`.
- First confirm the cause against the live tree and log. Then record each launch where it happens, as one append-only row through the build-log recorder, carrying the admission wait from the memory queue's own record. Both the `hana_shot.py` path and the `verify.sh build` path write it. Backfill from 10-15 onward where stored records allow it, and report the count.
- A cause in resources' launch hook goes to the showrunner with evidence; it is not fixed here.

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
