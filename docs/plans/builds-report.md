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

### Phase 3 — build-why says at once why the first waiting step waits, from admission's own numbers  · status: done

#### As-built

- `memory_admit.explain_queue() -> QueueExplanation` is a read-only record filled on the same locked pass as `read_queue` (`locked_queue_read`), from admission's own functions: host (free, total, earlyoom reserve, budget, pressure, or reading unavailable), each running step's charge with its `ChargeBasis`, totals, the head's budget, free and order checks with their numbers, and the line behind it. No SQLite is opened.
- `head_after_steps_end` reapplies `head_priority` and `queue_decision` (run-alone rule included) to the record minus named running steps, and returns `HeadAdmitsAfterEnd` or `HeadStillHeld` with its causes. `smallest_head_release` returns the smallest set by count, then charge, then Running order, judged by that verdict.
- Each waiting row records `need_source`, `need_runs` and `test_threads` at estimate time; an older row reads "source not recorded".
- `scripts/build_queue/build_why.py` prints the record in seven sections in under 1 s (0.3–0.8 s live under load); every number and its unit form one right-aligned cell and text is left-aligned, measured in terminal cells. A read failure prints `build-why: the admission state could not be read.` and exits 2.
- `/build:queue`'s head line prints from the same `QueueExplanation`; its Markdown number columns are `---:`, and the pane right-aligns numbers.
- `roster.unit_labels_for` also resolves Codex thread ids from run `mesh_roster.json` records, with no process scan, so build-why names every holder as the full roster does.

**Files:**
- `scripts/lint/memory_admit.py` — `explain_queue`, `QueueExplanation` and variants, `head_after_steps_end`, `smallest_head_release`, waiter need-source fields, `READ_HOST_ADMISSION_LIMITS` default
- `scripts/build_queue/build_why.py` — the command
- `scripts/build_queue/build_queue.py` — head line from `QueueExplanation`; aligned tables
- `scripts/message/roster.py` — `unit_labels_for` resolves Codex workers
- `mods/unit-board/hooks/queue.tsx` — number columns right-aligned
- `commands/build/queue.md` — names `build-why`
- `docs/as-built/build-memory-admission.md` — the explain record and need-source fields
- tests: `test_memory_admit.py`, `test_build_why.py`, `test_build_queue.py`, `test_roster.py`, `mods/unit-board/tests/queue.test.ts`

**Binds later work:** release advice and the head line only print admission's answer; no caller repeats budget or free arithmetic. build-why uses `unit_labels_for`, never the 4–7 s full roster.

**Gotchas:** origin/builds-resources' Phase 14 rewrites `queue_rows`; on merge, take its `owner_live` + `reservation_lifetime` form (one hunk).

### Phase 4 — App launches reach the build log again, with their memory wait  · status: done

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

**Depends on:** Phase 2 (both edit `scripts/buildlog/`; this phase reads index rows).

**Constraints from prior phases:** Phase 2: the index never rewrites a source log.

**Acceptance gate:** the buildlog tests green; basedpyright 0/0/0. Live: after one `/hana_shot` on this unit's port, the build log has one new launch row, and `/build:report` counts it.

**Ruling (showrunner, 2026-10-30 00:4x EDT):** a launch takes no memory admission by design (resources Phase 6, "BRP launch skips memory wait"), so a launch row has no wait to carry. The report shows its `mem_wait_s` as "no admission", never "not recorded", so no reader takes it for a lost value. Rows start from now; no backfill is required. The resources launch-hook subshell defect stays with resources.

### Phase 4b — A failed step's record survives a failed save, and a rerun finishes it  · status: todo

#### Work Order

**Goal:** when saving a failed step's log fails, the source log and peak file stay on disk, and the next run of the recorder finishes the record with no duplicate row.

**Spec:**
- Defect (rerun-sweep row `scripts/buildlog/record.py:315`, `~/.claude/docs/plans/sdlc-rerun-sweep.md`, builds-unit section; showrunner order 2026-10-30 00:20 EDT): the save of a failed step's log (near line 280) can fail after the `finally` block has already deleted the source log and the peak file. A rerun then has nothing to record from, and the step's record is lost.
- Fix: delete the sources only after the save is durable (written and renamed into place). A rerun that finds the sources and no saved record finishes the record; a rerun that finds the saved record and the sources deletes the sources and adds no second row.
- First confirm the order of save and delete against the live tree.

**Files:**
- `scripts/buildlog/record.py` — save, then delete; a rerun finishes a half-done record
- `scripts/buildlog/test_record_rerun.py` — a stop at each point, then a rerun

**Workers:** 1 writer + 1 tester.
- `impl2` — `record.py`
- `test2` — `test_record_rerun.py`

**State:** failed-step source logs, peak files, saved step logs, build-log rows

**Depends on:** Phase 4 (both edit `scripts/buildlog/record.py`).

**Constraints from prior phases:** Phase 2: the index never rewrites a source log.

**Acceptance gate:** a test stops the recorder after the save, after the row and before each delete, then reruns it: each case ends with one row, one saved log and no source files. The buildlog tests green; basedpyright 0/0/0.

### Phase 4c — Each scoped test step records its test-phase memory peak  · status: todo

#### Work Order

**Goal:** every scoped nextest step from `verify`, `cargo-port` and `validate_ci` (full and filtered, terminal and tee) records `test_phase_peak_bytes`: the step scope's largest `memory.stat` `anon`, in bytes, over its test phase.

**Spec:**
- Why (showrunner order 2026-10-30, for resources' parked test-phase charge, its Phase 15): today's 60 s machine samples cannot show a test peak per step.
- Spec source: line 20 of `/tmp/claude/unit/abea5ffb-4b99-499e-ae41-7ebb04630f9f/phases/15/impl_summary_impl.txt`.
- Sample once per second, from the first Cargo-profile `deps/` test binary seen in the scope until the scope ends. Count all parallel test processes in the scope.
- Put the sampling in the existing per-step scope script (`BUILDLOG_SCOPE_SH`, `scripts/lint/invoke.sh:88`). Read the value with the existing `sample.anon_bytes`. Start no second sampler.
- On release, persist these with the call/step identity: `test_phase_peak_bytes`, `test_phase_started_at` (epoch seconds), the actual `test_threads`, the repository and the filter family. A step with no test-phase mark records unknown, never 0.
- Also persist the charge admission assigned at test-phase entry, with its history cutoff, form and contributing widths. Read it from admission's own record; never compute it. If admission has no such field, record unknown and tell the showrunner; resources adds the field in its follow-up.
- Measure the sampler's CPU cost per step; the checkpoint notice names it.

**Files:**
- `scripts/lint/invoke.sh` — the scope script samples the test phase (owner verify-unit; trial-merge against `origin/natedev-verify-token` and name verify-unit with that tip in the notice's also-touches line)
- `scripts/buildlog/record.py` — persist the fields on release
- `scripts/buildlog/test_test_phase_peak.py` — first test binary starts the window, peak over parallel processes, no mark gives unknown, admission field absent gives unknown

**Workers:** 1 writer + 1 tester.
- `impl2` — `invoke.sh`, `record.py`
- `test2` — `test_test_phase_peak.py`

**State:** step records (new fields), the scope's peak/sample files

**Depends on:** Phase 4b (both edit `scripts/buildlog/record.py`).

**Constraints from prior phases:** Phase 2: the index never rewrites a source log.

**Acceptance gate:** the buildlog and `scripts/lint/test_invoke_scope.py` tests green; basedpyright 0/0/0. Live: one filtered `verify.sh test` step gets a `test_phase_peak_bytes` value, and the notice states the sampler's CPU cost per step.

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

**Constraints from prior phases:** Phase 1: a selector refusal is `memory_admit.UnmatchedWaiter`, printed on stdout with exit 3; the mod forwards only that. Phase 3: a waiting row's `session_id` is the writer's `CODEX_THREAD_ID` first, then `CLAUDE_CODE_SESSION_ID` (`waiter_identity`), so the identity `bump_selector` returns must be read from the row, never from the caller's environment; `read_queue` and `explain_queue` share one locked pass (`locked_queue_read`); the /build:queue head line prints from `QueueExplanation` and does no arithmetic. When origin/builds-resources' Phase 14 is merged, `queue_rows` keeps a reservation by `reservation_lifetime`, not by a live owner.

**Acceptance gate:** `python3 -m unittest scripts.build_queue.test_build_queue scripts.lint.test_memory_admit` green (prefix `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY`); basedpyright 0/0/0 on the changed files.
