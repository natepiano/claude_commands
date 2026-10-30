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
- `scripts/hana_shot/test_hana_shot.py` — `BuildLogIsolatedTest` gives every test its own `BUILDLOG_DIR`; a test proves the real log stays unchanged
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

#### As-built

- `record.py step` writes a `PendingStep` record (`pending-steps/<row_key>.json`) with the original id, the full record context and `month_offset` before any save. `PendingStep` is its own type, apart from `LaunchStep` and `PublishedLaunch`.
- `_finish_step` owns the order: save the gzip log, append the row to the host's monthly file, delete the held log and the peak file, then rename the record to `<row_key>.done`. The sources stay on disk until the row is durable.
- Replay rules: a rerun finishes an existing `.json` and records nothing new. A call that names a held log or peak file and finds `.done` records nothing. A call that names no source always records. Recovery scans the monthly file only from `month_offset` and counts a row only on a newline-terminated line.
- `launches.collect()` drains `pending-steps/*.json` and deletes `.done` markers older than `STEP_COMPLETION_MAX_AGE_SECONDS` (2 days).
- `store.append_line` adds a missing final newline under its flock before it appends.
- `sync.py` pulls only the host folders `record.SYNC_HOSTS` names, minus this host. Only natedev pushes `ci/`, and no host pulls it.
- `report.recorder_health_lines(day)` prints `N records not published yet; M recorder errors since <time>` for this host, only when N or M is above 0.

**Files:**
- `scripts/buildlog/record.py` — `PendingStep`, `_finish_step` order, replay rules, `SYNC_HOSTS`
- `scripts/buildlog/store.py` — `append_line` tail repair
- `scripts/buildlog/sync.py` — host-folder allow-list pull, natedev-only `ci/` push
- `scripts/buildlog/launches.py` — pending-step drain, `.done` expiry
- `scripts/buildlog/report.py` — local recorder health line
- `scripts/buildlog/test_record_rerun.py`, `test_store.py`, `test_sync.py` — stop-and-rerun, health line and sync allow-list cases

**Binds later work:** A step row reaches the log only through `record.py`'s `PendingStep` record (`pending-steps/<row_key>.json`, the host's monthly file, `month_offset`, the `.done` replay proof, `store.append_line`); every new row field must be in the pending record before the save starts. A source file the row needs (held log, peak file, any new sidecar) is deleted only by `_finish_step` after the row is durable. `record.SYNC_HOSTS` names the host folders sync carries; a file that must cross machines sits inside a host folder. `report.recorder_health_lines(day)` is the local recorder health line.

**Gotchas:** A named peak file that never existed is the normal `fallback` slice, so absent sources never prove a finished step. `_finish_step` sets the `.done` mtime at finish time, so a late finish does not expire at once.

**Ruled out:** one file per step row (index cost); source absence as proof of a finish.

### Phase 4c — Each scoped test step records its test-phase memory peak  · status: done

#### As-built

- The one admission sampler `buildlog_sample_anon` in `scripts/lint/invoke.sh` also opens a test-phase window on the first tick when a running executable in the step's scope has a direct parent folder named `deps` (`/deps/[^/]+$`). This works under any target folder name, with or without a target-triple level. Build scripts run from `<profile>/build/<pkg>-<hash>/` and do not open it. One `readlink` per tick covers all scope processes. The peak is the scope's `memory.stat` `anon`, so it counts all parallel tests.
- The sampler writes the window's anon peak and start time (epoch seconds) to the sidecar `$BUILDLOG_PEAK.test-phase.json` (temp + `mv`). It also writes the context from `record.py test-phase-context RESERVATION ARGV...`, read once before the loop. The same loop still writes admission's `.max` and `.wave` files.
- `record.py step` reads `BUILDLOG_TEST_PHASE_FILE` and validates the sidecar into `PendingStep.test_phase` (`TestPhaseRecord | NoTestPhaseRecord | UnreadableTestPhaseRecord`) before the save. An absent sidecar records unknown (null). An unreadable one keeps the sources and the pending record (`test_phase_read_error`) for a retry and writes one `pending record <path>` line to `errors.log`. Only `_finish_step` and the invalid-argument cleanup delete the sidecar.
- Row fields: `test_phase_peak_bytes`, `test_phase_started_at`, and the typed `TestPhaseContext` (`repo_identity`, `test_scope`, `package`, `filters`, `call_id`, `test_threads`). `test_threads` comes from the nextest flag first, then `NEXTEST_TEST_THREADS`, through admission's one parser `memory_admit.test_threads`. `test_phase_charge` records null: admission has no test-phase entry-charge field yet.
- Sampler CPU cost: about +0.36 CPU-s per accelerated 60-tick fixture step.

**Files:**
- `scripts/lint/invoke.sh` — the shared sampler opens and writes the test-phase window; `buildlog_end` passes `BUILDLOG_TEST_PHASE_FILE` to the recorder
- `scripts/buildlog/record.py` — `test-phase-context` command, the test-phase record types, the sidecar in `PendingStep` and its cleanup
- `scripts/buildlog/test_test_phase_peak.py` — window start, parallel peak, unknown cases, custom target folders, build-script negative, recovery after stops, width narrowed from 32 to 8
- `scripts/lint/test_invoke_scope.py` — the shared sampler still writes `.max` and `.wave`

**Binds later work:** an unreadable test-phase sidecar is a third cause of a held pending step and uses the same `pending record <path>` error line. `record.py` imports `memory_admit` once at module load, so every module that imports `record` loads it too. `test_test_phase_peak.py` composes the `RecordRerunTests` fixtures. `test_phase_charge` stays null until admission records a test-phase entry charge.

**Gotchas:** only a sidecar that does not exist is absent; a read or validation error holds the step pending. `memory_admit` imports `record` back through `importlib` and must not use `record` attributes while it loads. invoke.sh runs the installed `~/.claude/scripts/buildlog/record.py`, so the live check (one filtered `verify.sh test` step gets `test_phase_peak_bytes`) runs only after the change lands on main; it is still open.

**Ruled out:** a matcher that needs a folder literally named `target` (misses `CARGO_TARGET_DIR`); a match on any `/deps/` ancestor (a build script under such a path opened the window); reading the sidecar at finish time (a transient read error published null); a second sampler or a change to admission's release API.

### Phase 4d — Recorder health covers both hosts  · status: done

#### Work Order

**Goal:** `/build:report` on either host shows recorder health for each machine, natedev and the Mac: records the recorder has not finished and recorder errors since the report day's local midnight, with the host that holds them and the command that recovers them there.

**Spec:**
- Why (phase 4b contract review, showrunner ruling 2026-10-30 "4d"): the 4b health line reads only the local `errors.log` and pending folders, and sync carries neither, so a failed Mac save or publish never shows on natedev.
- One recorder-health model (new `scripts/buildlog/health.py`) owns the calculation. Move the calculation out of `report.recorder_health_lines()` (near line 1867); do not copy it. The producing host evaluates pending-path existence itself (an error for a pending record that was later finished does not count) and keeps the remaining error timestamps; the reader applies its own local-midnight boundary and the one formatter.
- One snapshot per machine, written atomically (temp + rename) into that machine's current host folder, so sync carries it. `record.SYNC_HOSTS` (near line 82) names three folders for two machines: map the historical Mac folder names to the one Mac machine, so the Mac counts once and no extra host shows as unknown.
- Refresh: only natedev runs `buildlog hourly` (`cli.hourly()`, `scripts/buildlog/cli.py` near line 211), and its sync does not collect on either side. Before the transfer, sync writes the local snapshot and has the Mac write its own snapshot inside the ssh call sync already makes (no extra connection). A failed snapshot write goes to `errors.log` and leaves the old snapshot, which then goes stale.
- Freshness: the snapshot holds `generated_at`. Named states: fresh (generated within two hourly sync periods of now), stale (older), unavailable (absent or unreadable). Do not compare with `sync.json`, which the Mac does not update on a push. A stale or unavailable machine shows as unknown, never as 0.
- Label: a pending record stays after the row is saved when its cleanup fails, so "not published yet" is wrong for it. The line reads "records the recorder has not finished" for both cases.
- One line per machine with a nonzero count or a non-fresh state, naming the machine and its recovery command (`buildlog launches`, which `buildlog report` also runs), e.g. `Mac: 2 records the recorder has not finished; 1 recorder error since 00:00 EDT — run buildlog launches on the Mac`. Clean, fresh machines add no line.

**Files:**
- `scripts/buildlog/health.py` — the one recorder-health model: calculate, write and read the snapshot, freshness state
- `scripts/buildlog/launches.py` — `collect()` writes this host's snapshot
- `scripts/buildlog/report.py` — one line per machine through the shared model
- `scripts/buildlog/sync.py` — refresh both snapshots before the transfer
- `scripts/buildlog/cli.py` — the hourly path, when the refresh needs it
- `scripts/buildlog/test_recorder_health.py` — a Mac failure reaches natedev's report without a report run on the Mac; a publish failure and a cleanup failure both count; a failed snapshot write cannot replace current failure data with a clean snapshot; fresh, stale and unavailable states; both Mac folder names count the Mac once; peer failure shows the host and its command, then recovery on the peer clears the line
- `scripts/buildlog/test_sync.py` — the refresh runs before the transfer and adds no ssh connection
- `scripts/buildlog/test_record_rerun.py` — keep the existing failed-save, persistent-publish, recovery and clean-log health cases green through the moved model

**Workers:** 1 writer + 1 tester.
- `impl2` — `health.py`, `launches.py`, `report.py`, `sync.py`, `cli.py`
- `test2` — `test_recorder_health.py`, `test_sync.py`, the affected health fixtures in `test_record_rerun.py`; owns the shared buildlog gate lane (impl2 type-checks only its own files and hands the gate to test2)

**State:** the per-machine health snapshot

**Depends on:** Phase 4b (health line, sync allow-list).

**Constraints from prior phases:**
- Phase 4b: sync pulls only the host folders `record.py` names, minus this host; `ci/` is pushed by natedev only. `.done` stays internal, a bounded replay proof. Cleanup of admission files already pulled from the Mac belongs to resources.
- Phase 4c: an unreadable test-phase sidecar now also holds a step before its save. Its `pending-steps/<row_key>.json` carries `test_phase_read_error`, and each failed retry writes the same `pending record <path>` line to `errors.log`. The health model counts it like every other pending record, with no case of its own, and never reads the sidecar.
- Phase 4c: `record.py` imports `scripts/lint/memory_admit.py` when it loads, so every module that imports `record` (`health.py`, and the Mac's snapshot write inside the ssh call) loads it too.
- Phase 4c: `test_test_phase_peak.py` composes `test_record_rerun.RecordRerunTests` and its fixtures. A change to those health fixtures keeps that file green.

**Acceptance gate:** the buildlog tests green; basedpyright 0/0/0. Live: after one `buildlog hourly` on natedev, with no report run on the Mac, natedev's report shows a Mac line for a recorder error written on the Mac for the test. The ssh call runs the Mac's installed `~/.claude` scripts, so the live check runs after the change lands on main and the Mac's `~/.claude` holds it.

**Assertion-Change:** `scripts/buildlog/test_record_rerun.py` — `assert_health_line` (regex and report filter), `assert_persistent_publish_error_recovers`, `test_clean_log_adds_no_recorder_health_line` and `test_health_uses_the_users_local_midnight_and_existing_local_time_format` expect the new per-machine line (`natedev: … records the recorder has not finished; … — run buildlog launches on natedev`, singular for 1) in place of `records not published yet`; the Spec's label and line form. `assert_health_line` takes an exact error count (was the pattern `[1-9][0-9]*` with `errors?`) and filters report rows on `the recorder has not finished`, so singular rows keep the full equality check; the persistent-retry case expects `attempt + 2` errors and the local-midnight case `1 recorder error since …` (was `1 recorder errors since …`). Each change pins a count the old pattern left loose.
**Assertion-Change:** `scripts/buildlog/test_sync.py` — `test_non_255_ssh_exit_uses_printed_success` expects `ssh, rsync, rsync` (was `ssh, rsync`) and `test_only_natedev_pushes_ci_and_no_host_pulls_it` expects one push on the Mac too, without `ci/` (was none): the snapshot now creates the Mac's host folder, so its push runs.

### Phase 4e — build-why and /build:queue show how long the first step has been head, who bumped which steps since it joined, the order window and a cargo-token wait  · status: done

#### Work Order

**Goal:** For the first waiting step, `build-why` and `/build:queue` show four facts next to its queue wait: how long it has been head, each bump since it joined (who bumped which step), the strict or backfill window now and when it ends, and a cargo-token wait. Each fact comes from admission's own records. A fact that admission does not record shows as `unknown`.

**Spec:**
- Why (showrunner request, 2026-10-30 06:4x EDT; data in `/tmp/claude/unit/abea5ffb-4b99-499e-ae41-7ebb04630f9f/pressure_followup_report.md`): a check step showed "waiting 158 min" (queue entry 03:52:26, admission 06:31:58), but it was head only for seconds, at about 06:28 and again at about 06:31. 88 queue bumps of other steps and the strict/backfill cycle held it, and a 154 s cargo-token wait came at the end. `build-why` and `/build:queue` show only the current reason and the wait since queue entry (`Waiter.since`), so none of this showed.
- Source rule: every number comes from admission's own records. The reader computes nothing that admission already decides (head order, priority, window, budget, free memory). A fact with no record shows as `unknown`, never as `0` and never as a value taken from a different field.
- One shared reader: new `scripts/build_queue/head_facts.py`. `read_head_facts(reading: memory_admit.QueueRead) -> HeadFacts | memory_admit.NoWaiter` takes the one locked `QueueRead` that both commands already read (`memory_admit.locked_queue_read`), and adds the bump-log read below. The same module holds one formatter for each fact. `build_why.py` and `build_queue.py` both call it. Neither keeps a copy of a rule, a field read or a phrase.
- Fact 1, head time. Admission source: missing today. The Waiter row (`scripts/lint/memory_admit.py:260-282`) has `since` (queue entry; a token give-back keeps it through `BUILDLOG_MEM_WAIT_SINCE`) and `priority_since`. `priority_since` is not head time: `check()` sets it (:1554-1559) only when `head_priority` (:1291-1304) gives priority; `bump_waiter` sets it at a bump (:2022); it stays on a row that a later bump moves back; a token give-back deletes it with the row. No field records when the row became first in `waiter_order`. Show it as the Decision below settles: `head for <n> min (since HH:MM:SS)` from the new field, else `head for: unknown`. Never show `since` or `priority_since` as head time.
- Fact 2, bumps since it joined. Admission source: the build-log `queue_bump` rows that `memory_admit.bump_waiter` writes under the queue lock through `record.queue_bump` (`scripts/lint/memory_admit.py:2026`, `scripts/buildlog/record.py:958-964`). Fields: `at` (ISO UTC), `actor` (session id or `user:<name>`), `session` (the bumped step's session id or name), `step`, `folder`, `old_position`. The bumps since it joined are the rows with head `Waiter.since` <= `at` <= `QueueExplanation.now`. A bump row holds no waiter row key, so the reader names each bumped step only by its recorded `session`, `step` and `folder`. It never marks a row as "this step".
  - Read the local host's monthly files (`store.host_file(store.host_name(), t)`, `scripts/buildlog/store.py:55-56`), from the month of `since` to the month of now. Read each file backward from its end in 1 MiB blocks. Parse only complete, newline-terminated lines that hold `"kind":"queue_bump"`. Stop at the first bump row with `at` before `since`: bump rows are appended under the admission lock in `at` order. A missing month file has no rows.
  - A read error, a malformed bump line, or more than 32 MiB read before the stop makes the whole fact `unknown`. A partial count never shows. No SQLite: the index lags the live log.
  - `FORMER_FIELDS` (Phase 2) maps only `step` and `call` rows. Bump rows need no mapping.
  - Group the rows by (`session`, `step`, `folder`): count, actors, last `at`. Order: count descending, then last `at` descending.
- Fact 3, order window. Admission source: `HeadExplanation.order` (`StrictUntil.deadline` or `BackfillUntil.deadline`, from `head_window`, `scripts/lint/memory_admit.py:385-396`) and `HeadExplanation.has_priority` (:543-552, filled at :1834-1838).
  - `has_priority` with `StrictUntil`: `strict until HH:MM:SS (<m> min <s> s left): younger steps wait`.
  - `has_priority` with `BackfillUntil`: `backfill until HH:MM:SS (<m> min <s> s left): younger steps that fit may start`.
  - No `has_priority`: `no head priority: younger steps that fit may start`, with no change time. `queue_decision` (:1307-1319) holds younger steps only under a `HeadPriority` with a `StrictUntil`, and admission gives priority on a later pass only when the fit check in `head_priority` passes.
  - This replaces build-why's `strict until ...; head priority is not active` text (`scripts/build_queue/build_why.py:189-197`) and the head line's `younger steps wait until` text (`scripts/build_queue/build_queue.py:108-112`), which shows a strict window also when no priority holds.
- Fact 4, cargo-token wait. Admission source: missing today. After admission, `buildlog_wait_for_memory` in `scripts/direct/verify.sh` (lines 2629-2634) tries the cargo token once. On a miss it calls `buildlog_release_memory 0`, waits in `acquire_token`, then joins the memory queue again with its first `since`. In that gap the step has no waiter row and no reservation, and `memory_admit.release` (:2032-2060) records no reason, so admission has no record of the wait. Show it as the Decision below settles. build-why prints one `Cargo token:` line for each finished wait of a step now waiting; `Cargo token waits: none` when the record exists and no waiting step has one; `Cargo token waits: unknown` when the record is absent or unreadable. A step in a wait now has no waiter row, so it is not in the view; a running step's waits are not shown (director ruling, 2026-10-30).
- build-why (`render_explanation`, `scripts/build_queue/build_why.py:148-217`): after the `First in line` line (lines 182-186), print in this order:
  - `Head for: ...` (fact 1).
  - `Bumps since it joined at HH:MM:SS: <n>`, then an `aligned_table` with columns Step, Session, Folder, Bumps, By, Last bump. Bumps is a right-aligned number cell. At most 8 rows, then `and <k> more steps with <j> bumps`. With no bump row: `Bumps since it joined at HH:MM:SS: none`. With an unknown result: `Bumps since it joined: unknown (<reason>)`.
  - The `Order rule:` line from fact 3.
  - The cargo-token line from fact 4.
  - Put the session ids of actors and bumped steps into the one `roster.unit_labels_for` call that `main()` already makes (lines 225-228). Make no second label read, and never read the full roster.
  - A failed bump read shows `unknown`. It never turns into the `build-why: the admission state could not be read.` exit 2.
- `/build:queue` (`scripts/build_queue/build_queue.py`): `head_line` (lines 157-180) adds the short forms: `head for <n> min` or `head for: unknown`; `<n> bumps since it joined, most: <folder> <step> (<count>)`, `no bumps since it joined`, or `bumps: unknown`; and the fact 3 window text in place of the current text. The head line also adds `cargo token waited <n> s`, `no cargo token wait`, or `cargo token: unknown` for the head step (review finding F003). The `Stalled:` rule (lines 175-177) does not change. No cargo-token rows go into the Waiting table: the record holds only finished waits (director ruling, 2026-10-30). The `QueueSnapshot` JSON shape does not change, so the mod pane needs no edit.
- `docs/as-built/build-memory-admission.md` line 85 (the build-why paragraph) names the four head facts and their sources in the same commit.
- Speed: build-why stays under 1 s live. The bump read is one backward scan for each run.

**Decision (showrunner, 2026-10-30): Option A.** Resources adds `head_since: float` on the Waiter row and a cargo-token give-back record in its own follow-up (resources Phase 26): one line of `kind` `token_gap` in `${BUILDLOG_DIR}/admission/queue_times.jsonl` for each finished wait, with `session_id`, `step`, `pid`, `start_time`, `token_left_at` and `token_back_at`, written when the step joins the queue again. The same file also holds other kinds of lines (resources Phase 20 lifecycle events), which this phase skips. A gap belongs to a waiting step with the same `step`, the same non-empty `session_id` (else the same `pid` and `start_time`) and `token_left_at` at or after the step's `since`; every line of every kind carries `at` (its write time; one append function writes the file in `at` order under the admission lock and, past 8 MiB, trims it to the last 14 days, agreed with resources 2026-10-30), so the backward scan stops at the first line whose `at` is before the oldest waiting step's `since`, and a line without `at` makes the fact unknown (`scripts/lint/memory_admit.py`, `scripts/lint/memory_gate.sh`, `scripts/direct/verify.sh` near 2630; not this phase's files). This phase reads each field when present and shows `head for: unknown` and `Cargo token waits: unknown` when absent, so it does not wait for resources. Never derive either fact from `since` or `priority_since`.

**Files:**
- `scripts/build_queue/head_facts.py` — new: the shared reader (`read_head_facts`, `HeadFacts`, the bump-log scan) and one formatter for each fact
- `scripts/build_queue/build_why.py` — prints the four facts after `First in line`; adds bump actor and session ids to the one label call
- `scripts/build_queue/build_queue.py` — `head_line` short forms and fact 3 window text; cargo-token rows in the Waiting table
- `docs/as-built/build-memory-admission.md` — the build-why paragraph names the four facts and their sources
- `scripts/build_queue/test_head_facts.py` — new: bump rows at `since` count and before `since` do not; rows after `QueueExplanation.now` do not count; the scan stops at the first older bump row; month boundary; missing month file gives none, not unknown; torn last line ignored; malformed bump line and the 32 MiB limit give unknown; grouping and order; head time unknown without the field, and never from `since` or `priority_since`; the three window texts; token wait unknown without a record
- `scripts/build_queue/test_build_why.py` — line order; 8-row bump table cap; Bumps cell right-aligned; one `unit_labels_for` call that holds actor and bumped-session ids; a failed bump read prints unknown and exits 0
- `scripts/build_queue/test_build_queue.py` — head line short forms; no strict text without priority; `QueueSnapshot` keys unchanged

**Workers:** 1 writer + 1 tester.
- `impl3` — `head_facts.py`, `build_why.py`, `build_queue.py`, `docs/as-built/build-memory-admission.md`
- `test3` — `test_head_facts.py`, `test_build_why.py`, `test_build_queue.py`, written from the Spec; owns the gate lane (impl3 type-checks only its own files and hands the gate to test3)

**State:** none. The phase reads the admission folder and the build log, and writes neither.

**Depends on:** Phase 3 (`QueueExplanation`, `HeadExplanation.order` and `has_priority`, `build_why.py`) and Phase 4a (the `queue_bump` row and its locked `old_position`). It does not depend on Phase 4d: 4d edits only `scripts/buildlog/` (`health.py`, `launches.py`, `report.py`, `sync.py`, `cli.py` and their tests), and 4e edits no file there; it only reads `store.host_file` and the bump rows. The resources fields are not a dependency either: an absent field shows `unknown`.

**Constraints from prior phases:**
- Phase 1: `build_queue.py --json` prints one `QueueSnapshot`; the mod pane reads it. The head line keeps its `{"text", "tone"}` shape. Each cell is formatted once, in the user's zone, and the text tables and the JSON come from the same row values.
- Phase 3: `explain_queue` and `locked_queue_read` are one read-only locked pass and open no SQLite. The head line and release advice print only admission's answer; no caller repeats budget or free arithmetic. build-why uses `roster.unit_labels_for`, never the 4–7 s full roster. build-why runs in under 1 s (0.3–0.8 s live under load). A number and its unit form one right-aligned cell, text is left-aligned, and widths are measured in terminal cells (`aligned_table`). A read failure prints `build-why: the admission state could not be read.` and exits 2.
- Phase 3 gotcha: origin/builds-resources' Phase 14 rewrites `queue_rows`; on merge, take its `owner_live` + `reservation_lifetime` form.
- Phase 4a: a waiter row key is its file name `{pid}-{start_time}.waiting`, read from `QueueRead.ordered`. The `queue_bump` row's `old_position` is the locked position, not the position the user saw. `memory_admit.py` is resources-unit's and read only in this phase (Delegation Context, Ownership).
- Phase 2: a reader that opens the `.jsonl` logs directly maps `FORMER_FIELDS` for `step` and `call` rows; the index never rewrites a source log.
- Phase 4b: `store.append_line` adds a missing final newline under its flock before it appends; a row counts only on a newline-terminated line.

**Acceptance gate:**
- From the worktree root: `python3 -m unittest scripts.build_queue.test_head_facts scripts.build_queue.test_build_why scripts.build_queue.test_build_queue` green.
- `basedpyright scripts/build_queue/head_facts.py scripts/build_queue/build_why.py scripts/build_queue/build_queue.py scripts/build_queue/test_head_facts.py scripts/build_queue/test_build_why.py scripts/build_queue/test_build_queue.py` footer reads `0 errors, 0 warnings, 0 notes`.
- Live on natedev, with at least one `.waiting` row in `~/.local/state/buildlog/admission/`. From the worktree root, type `time ~/.claude/scripts/lib/py scripts/build_queue/build_why.py`. See:
  - `real` under 1.0 s.
  - After `First in line`: `Head for: unknown` (or `head for <n> min (since HH:MM:SS)` once the resources field exists).
  - `Bumps since it joined at HH:MM:SS: <n>` and its table. Then type `s=$(TZ="$(~/.claude/scripts/lib/py ~/.claude/scripts/lib/user_zone.py)" date -d HH:MM:SS +%s); grep '"kind":"queue_bump"' ~/.local/state/buildlog/natedev/$(date +%Y-%m).jsonl | python3 -c 'import sys, json, datetime as d; s = float(sys.argv[1]); print(sum(d.datetime.fromisoformat(json.loads(l)["at"]).timestamp() >= s for l in sys.stdin))' "$s"` with the printed join time. It prints the same `<n>`, or more only if a bump came between the two commands (a join before midnight: use the date with the time).
  - `Order rule:` with `strict until HH:MM:SS`, `backfill until HH:MM:SS` and the time left, or `no head priority`.
  - `Cargo token waits: unknown` without the record, `Cargo token waits: none` with it and no matching gap, or one `Cargo token:` line for each matching gap.
- Then type `~/.claude/scripts/lib/py scripts/build_queue/build_queue.py`. See the head line above the Waiting table with `head for`, the bump count with its `most:` step, and the same window text as build-why. Make no bump for the check.

**Assertion-Change:** `scripts/build_queue/test_build_queue.py` — `test_head_reason_keeps_required_free_threshold_when_both_limits_hold`, `test_waiting_row_names_head_and_age_with_local_time`, `test_head_free_threshold_names_step_growth_and_reserve`, `test_head_reason_names_measured_budget_limit_and_start_level` and `test_head_reason_names_only_free_memory_when_budget_passes` expect `no head priority: younger steps that fit may start` (was `younger steps wait until HH:MM`); `test_head_backfill_label_uses_the_since_zone_and_becomes_strict_at_ten_minutes` sets `has_priority=True` and expects the fact 3 `backfill until …`/`strict until … (… left)` texts. Fact 3 replaces the old window text.

