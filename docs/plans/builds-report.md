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

### Phase 4 — App launches reach the build log again  · status: done

#### As-built

- Each `/hana_shot` start writes one launch row. `record.launch(state_path, worktree)` takes a lock and saves a pending record atomically: `_save_pending_launch` writes a temp file, syncs it with fsync and moves it into place with `os.replace`, under `pending_launches_root()` (`<BUILDLOG_DIR>/pending-launches`).
- `finish_pending_launch` then publishes the row through `write_launch_line` (hard link, fsync, directory sync). It checks that the row file is present, and only then unlinks the pending record. `launches.collect()` finishes each pending launch that a failed publish left.
- A launch takes no memory admission by design (resources Phase 6, showrunner ruling 2026-10-30). `report.py` `launches_section` shows "no admission" for each `step=launch` row, never "not recorded".
- The transcript backfill and the hana-shot cache scan are gone. Legacy BRP transcript rows stay in the log.
- `hana_shot.py` (owner screenshot-unit) loads the recorder by file path as `_hana_shot_buildlog_record` and calls it in its cleanup step. A recorder failure goes to `note_error`, and cleanup continues.

**Files:**
- `scripts/buildlog/record.py` — `hana_launch_record`, `write_launch_line`, `pending_launches_root`, `_save_pending_launch`, `finish_pending_launch`, `launch`
- `scripts/buildlog/launches.py` — `collect()` finishes pending launches
- `scripts/buildlog/report.py` — `launches_section` shows "no admission"
- `scripts/hana_shot/hana_shot.py` — one recorder call in cleanup
- `scripts/buildlog/test_launch_recording.py` — 16 tests
- `docs/as-built/buildlog-memory-stalls.md` — launch rows

**Binds later work:** a failed step's record that must survive a failed save uses the same durable-before-delete shape. It reuses `_save_pending_launch` and `finish_pending_launch`, and adds no second atomic-save routine.

**Gotchas:** hana_shot tests import `scripts.hana_shot` and run from the repo root: `python3 -m unittest scripts.hana_shot.test_hana_shot`. The live check is still open: after 05:00 EDT, one `/hana_shot` must add one row that `/build:report` counts as "no admission".

**Ruled out:** a launch memory wait (launches take no admission); a transcript backfill (rows start from the phase's landing).

### Phase 4a — A bump by shown name moves only the step it names  · status: done

#### As-built

- `build_queue.bump_selector(selector, reading: QueueRead, labels)` maps an exact shown name of one waiting step to `memory_admit.WaiterRowSelector(row_key, original_selector)`. `row_key` is the row's own file name, `{pid}-{start_time}.waiting`, read from `QueueRead.ordered`. The key comes from the row, never from the caller's environment.
- `memory_admit.bump_waiter(selector: str | WaiterRowSelector, actor)` finds that row again by file name under the queue lock and moves only it. If the row left the queue, it gives `UnmatchedWaiter` that names the original shown name, on stdout with exit 3; no other row moves.
- The `queue_bump` build-log row's `old_position` is the row's position under the lock.
- Tests cover a row removed between reads, a reordered queue, two shown names with a shared prefix (and a repeat bump), and a departed row key with another step in the same session. Waiter fixture files use `{pid}-{start_time}.waiting`.

**Files:**
- `scripts/build_queue/build_queue.py` — `bump_selector` makes the row-key hand-off
- `scripts/lint/memory_admit.py` — `WaiterRowSelector`; `bump_waiter` matches the row key under the lock
- `scripts/build_queue/test_build_queue.py` — race and prefix tests

**Gotchas:** a shown name with no match still goes to `bump_waiter` as a plain string, which then matches by position, session id, session name or folder. `old_position` is the locked position, not the position the user saw. `memory_admit.py` also-touches resources: trial-merge against `origin/builds-resources`.

**Ruled out:** a second waiter identity (the row file name is the key); session id alone (several waiters share one).

### Phase 4b — A failed step's record survives a failed save, and a rerun finishes it  · status: done

#### Work Order

**Goal:** when saving a failed step's log fails, the source log and peak file stay on disk, and the next run of the recorder finishes the record with no duplicate row.

**Spec:**
- Defect (rerun-sweep row `scripts/buildlog/record.py:315`, `~/.claude/docs/plans/sdlc-rerun-sweep.md`, builds-unit section; showrunner order 2026-10-30 00:20 EDT): the save of a failed step's log (near line 280) can fail after the `finally` block has already deleted the source log and the peak file. A rerun then has nothing to record from, and the step's record is lost.
- Fix: delete the sources only after the save is durable (written and renamed into place). A rerun that finds the sources and no saved record finishes the record; a rerun that finds the saved record and the sources deletes the sources and adds no second row.
- First confirm the order of save and delete against the live tree.
- No silent loss (showrunner, 2026-10-30, Phase 4 plan review): a launch whose pending record cannot be saved, and a pending launch or step whose publish keeps failing in `collect()`, today reach only `errors.log`. `/build:report` prints one line, `N records not published yet; M recorder errors since <time>`, only when N or M is above 0; a clean log adds no line.

**Files:**
- `scripts/buildlog/record.py` — save, then delete; a rerun finishes a half-done record
- `scripts/buildlog/sync.py` — the pull carries only the folders `record.py` names (the host folders); every other folder stays local (review finding, showrunner ruling 2026-10-30)
- `scripts/buildlog/launches.py` — `collect()` counts what it could not publish
- `scripts/buildlog/report.py` — the unpublished/errors line
- `scripts/buildlog/test_record_rerun.py` — a stop at each point, then a rerun; a failed pending save, a publish that keeps failing, then recovery; a clean log prints no line

**Workers:** 1 writer + 1 tester.
- `impl2` — `record.py`, `launches.py`, `report.py`
- `test2` — `test_record_rerun.py`

**State:** failed-step source logs, peak files, saved step logs, build-log rows

**Depends on:** Phase 4 (both edit `scripts/buildlog/record.py`).

**Constraints from prior phases:** Phase 2: the index never rewrites a source log. Phase 4: app launches publish through a pending record (`record._save_pending_launch`: temp, fsync, `os.replace`), then `finish_pending_launch` publishes the row, checks it is present and only then unlinks the pending record; `launches.collect()` finishes pending launches. Share those persistence helpers, but keep a pending step its own type, apart from `LaunchStep` and `PublishedLaunch`. `step()` makes a new random id on every call (`new_id`), so the pending step keeps the original id and the full record context; a finish from a fresh process with a different environment gives the same row. Name the entry point that finds unfinished steps. The Phase 4 launch recovery tests stay green.

**Acceptance gate:** a test stops the recorder after the save, after the row and before each delete, then reruns it: each case ends with one row, one saved log and no source files. The report line shows for a failed pending save and for a publish that keeps failing, and goes away after recovery; a clean log shows no line. The buildlog tests green; basedpyright 0/0/0.

### Phase 4c — Each scoped test step records its test-phase memory peak  · status: todo

#### Work Order

**Goal:** every scoped nextest step from `verify`, `cargo-port` and `validate_ci` (full and filtered, terminal and tee) records `test_phase_peak_bytes`: the step scope's largest `memory.stat` `anon`, in bytes, over its test phase.

**Spec:**
- Why (showrunner order 2026-10-30, for resources' parked test-phase charge, its Phase 15): today's 60 s machine samples cannot show a test peak per step.
- Spec source: line 20 of `/tmp/claude/unit/abea5ffb-4b99-499e-ae41-7ebb04630f9f/phases/15/impl_summary_impl.txt`.
- Sample once per second, from the first Cargo-profile `deps/` test binary seen in the scope until the scope ends. Count all parallel test processes in the scope.
- Put the sampling in the existing per-step sampler `buildlog_sample_anon` (`scripts/lint/invoke.sh` near line 172), which already writes admission's `.max` and `.wave` files from the scope's `memory.stat` `anon`. One loop writes both those files and the test-phase window; keep its atomic publication (temp + `mv`) and its exit when the parent ends. Start no second sampler.
- Admission release deletes its reservation and measurement files (`memory_admit.py` `release`, near line 2055), and both invoke.sh paths release before they call the recorder (near line 307). So invoke.sh writes the test-phase fields into the step's own sidecar before release, and copies admission's assigned charge there from admission's record, computing nothing; `record.py step` reads that sidecar. Admission's release API does not change (showrunner OK, 2026-10-30).
- On release, persist these with the call/step identity: `test_phase_peak_bytes`, `test_phase_started_at` (epoch seconds), the actual `test_threads`, the repository and the filter family. A step with no test-phase mark records unknown, never 0.
- Also persist the charge admission assigned at test-phase entry, with its history cutoff, form and contributing widths. Read it from admission's own record; never compute it. If admission has no such field, record unknown and tell the showrunner; resources adds the field in its follow-up.
- Measure the sampler's CPU cost per step; the checkpoint notice names it.

**Files:**
- `scripts/lint/invoke.sh` — the scope script samples the test phase (owner verify-unit; trial-merge against `origin/natedev-verify-token` and name verify-unit with that tip in the notice's also-touches line)
- `scripts/buildlog/record.py` — persist the fields on release
- `scripts/buildlog/test_test_phase_peak.py` — first test binary starts the window, peak over parallel processes, no mark gives unknown, admission field absent gives unknown, terminal and tee steps record after admission's files are gone
- `scripts/lint/test_invoke_scope.py` — the shared sampler still writes `.max` and `.wave`

**Workers:** 1 writer + 1 tester.
- `impl2` — `invoke.sh`, `record.py`
- `test2` — `test_test_phase_peak.py` and the affected cases in `scripts/lint/test_invoke_scope.py`

**State:** step records (new fields), the scope's peak/sample files

**Depends on:** Phase 4b (both edit `scripts/buildlog/record.py`).

**Constraints from prior phases:** Phase 2: the index never rewrites a source log.

**Acceptance gate:** the buildlog and `scripts/lint/test_invoke_scope.py` tests green; basedpyright 0/0/0. Live: one filtered `verify.sh test` step gets a `test_phase_peak_bytes` value, and the notice states the sampler's CPU cost per step.

### Phase 4d — Recorder health covers both hosts  · status: todo

#### Work Order

**Goal:** `/build:report` on either host shows the recorder health line for both hosts: records not published yet and recorder errors since the report day's local midnight, summed over natedev and the Mac.

**Spec:**
- Why (phase 4b contract review, showrunner ruling 2026-10-30 "4d"): the 4b health line reads only the local `errors.log` and pending folders, and sync carries neither, so a failed Mac save or publish never shows on natedev.
- Each host's `launches.collect()` writes a small health file into its own host folder (the folder sync already carries): pending count and the recorder-error timestamps the report needs. Atomic write (temp + rename).
- The report sums the local counts and each peer host folder's health file. A peer whose file is absent or older than its last sync shows as unknown, never as 0.
- One place reads and one place writes the health file; the 4b health function uses it for the local host too.

**Files:**
- `scripts/buildlog/launches.py` — write the host health file in `collect()`
- `scripts/buildlog/report.py` — sum health over hosts
- `scripts/buildlog/test_recorder_health.py` — Mac failure shows on natedev, absent peer file shows unknown, clean hosts add no line

**Workers:** 1 writer + 1 tester.
- `impl2` — `launches.py`, `report.py`
- `test2` — `test_recorder_health.py`

**State:** the host health file

**Depends on:** Phase 4b (health line, sync allow-list).

**Acceptance gate:** the buildlog tests green; basedpyright 0/0/0. Live: after a sync, natedev's report counts a Mac recorder error written for the test.
