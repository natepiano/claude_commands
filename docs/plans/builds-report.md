# builds-report

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `/build:queue` opens a pane from the unit-board mod, with no model turn, as `/accounts-board` does, and says at once why the head step waits; the build log index stops rewriting its source logs; app launches reach the build log again with their memory wait.

> **As-built disposition: amend** — `docs/as-built/unit-board.md` (the shared table pane and `/build:queue`); `docs/as-built/build-memory-admission.md` (Queue API); `docs/as-built/buildlog-memory-stalls.md` (index reads old field names); `docs/as-built/build-followups.md` (rebuild bins: the warm rule, `first build here`, the step build-key columns)

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

- `record.py step` writes a `PendingStep` record (`pending-steps/<row_key>.json`) with the original id, the full record context and `month_row_end` (the end of the last whole row; a record written before Phase 6 has only `month_offset` and scans from 0) before any save. `PendingStep` is its own type, apart from `LaunchStep` and `PublishedLaunch`.
- `_finish_step` owns the order: save the gzip log, append the row to the host's monthly file, delete the held log and the peak file, then rename the record to `<row_key>.done`. The sources stay on disk until the row is durable.
- Replay rules: a rerun finishes an existing `.json` and records nothing new. A call that names a held log or peak file and finds `.done` records nothing. A call that names no source always records. Recovery scans the monthly file only from `month_row_end` and counts a row only on a newline-terminated line.
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

**Binds later work:** A step row reaches the log only through `record.py`'s `PendingStep` record (`pending-steps/<row_key>.json`, the host's monthly file, `month_row_end`, the `.done` replay proof, `store.append_line`); every new row field must be in the pending record before the save starts. A source file the row needs (held log, peak file, any new sidecar) is deleted only by `_finish_step` after the row is durable. `record.SYNC_HOSTS` names the host folders sync carries; a file that must cross machines sits inside a host folder. `report.recorder_health_lines(day)` is the local recorder health line.

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

#### As-built

- `scripts/buildlog/health.py` owns recorder health. `calculate_snapshot()` runs on the machine that owns the data. It counts the `.json` files under `record.pending_launches_root()` and `record.pending_steps_root()`, and keeps each timestamped `errors.log` line except a `pending record <path>` line whose path no longer exists. It returns `RecorderHealthSnapshot(machine, generated_at, unfinished_records, error_timestamps)`.
- `write_snapshot()` writes `<host folder>/recorder-health.json` (`v: 1`) under an exclusive `flock` on `store.root()/recorder-health.lock`, through a temp file, fsync and `os.replace`. On any failure it writes `recorder health snapshot` to `errors.log`, keeps the old snapshot and returns False. `python health.py` runs it for this host.
- `launches.collect()` refreshes the snapshot at its end. `sync._sync_locked()` refreshes the local snapshot before the transfer. The Mac writes its own snapshot inside the mkdir ssh call that sync already makes, so sync opens no extra connection.
- `machine_for_host()` maps every non-natedev `record.SYNC_HOSTS` folder to `Machine.MAC`, so the Mac counts once. `read_snapshots()` returns one state per `Machine`: `FreshRecorderHealth` (at most `SNAPSHOT_MAX_AGE_SECONDS`, 2 h, old), `StaleRecorderHealth`, or `RecorderHealthUnavailable` (absent, unreadable or invalid). It skips a snapshot more than `SNAPSHOT_CLOCK_SKEW_SECONDS` (300 s) in the future. The newest usable Mac folder wins.
- `report.recorder_health_lines(day)` formats the states. It applies the reader's own local midnight through `errors_since()` and picks singular or plural nouns by count. A clean, fresh machine adds no line. Line forms:
  - `Mac: 2 records the recorder has not finished; 1 recorder error since 00:00 EDT — run buildlog launches on the Mac`
  - `natedev: recorder health unknown (snapshot stale; last updated <time>) — run buildlog launches on natedev`
  - `Mac: recorder health unknown (snapshot unavailable) — run buildlog launches on the Mac`

**Files:**
- `scripts/buildlog/health.py` — the recorder-health model: calculate, write, read, freshness states
- `scripts/buildlog/launches.py` — `collect()` refreshes this host's snapshot
- `scripts/buildlog/sync.py` — refreshes both snapshots before the transfer
- `scripts/buildlog/report.py` — `recorder_health_lines()`, one line per machine
- `scripts/buildlog/test_recorder_health.py` — model, freshness, Mac folder alias, lock and peer-failure cases
- `scripts/buildlog/test_sync.py`, `scripts/buildlog/test_record_rerun.py` — refresh order and the health line through the shared model

**Gotchas:**
- The lock file sits at `store.root()/recorder-health.lock`, outside the synced host folders.
- The snapshot creates the Mac's host folder, so the Mac's sync push now always runs.
- The Mac's snapshot comes from the Mac's installed `~/.claude` scripts. Until that copy holds `health.py`, natedev shows the Mac as unknown.
- `health.py` imports `record`, which loads `scripts/lint/memory_admit.py`.
- A stale or unavailable machine reads as unknown, never as 0.

**Ruled out:** freshness from `sync.json` (the Mac does not update it on a push); a separate ssh connection for the Mac refresh; the label "not published yet" (a pending record whose cleanup failed stays after its row is saved).

### Phase 4e — build-why and /build:queue show how long the first step has been head, who bumped which steps since it joined, the order window and a cargo-token wait  · status: done

#### As-built

- `head_facts.read_head_facts(reading: memory_admit.QueueRead) -> HeadFacts | memory_admit.NoWaiter` reads four facts for the first waiting step from the one locked `QueueRead`. `build_why.py` and `build_queue.py` call it and its formatters (`head_time_text`, `bump_text`, `bump_rows`, `bump_remainder`, `order_text`, `cargo_token_lines`, `cargo_token_text`, `session_ids`) and keep no copy of a rule, field read or phrase. The reader computes nothing admission decides. A fact with no record shows `unknown`, never `0`.
- Sources: head time is the Waiter `head_since` field (written by resources-unit) on the first row of `QueueRead.ordered`, used only when finite, >= 0 and <= now. Bumps are `queue_bump` rows (`at`, `actor`, `session`, `step`, `folder`, `old_position`) in this host's monthly build-log files, `since <= at <= now`, grouped by (`session`, `step`, `folder`) with count, actors and last `at`, ordered by count, then last `at`, both descending. The window is `HeadExplanation.order` (`StrictUntil`/`BackfillUntil` deadline) and `has_priority`. Cargo-token waits are `kind` `token_gap` lines in `admission/queue_times.jsonl` under the build-log root: `session_id`, `step`, `pid`, `start_time`, `token_left_at`, `token_back_at`, and `at` on every line of every kind.
- build-why, after `First in line`: `Head for: <n> min (since HH:MM:SS)` or `Head for: unknown`; `Bumps since it joined at HH:MM:SS: <n>` with an `aligned_table` (Step, Session, Folder, Bumps right-aligned, By, Last bump; 8 rows, then `and <k> more steps with <j> bumps`), or `: none`, or `Bumps since it joined: unknown (<reason>)`; `Order rule:` with `strict until HH:MM:SS (<m> min <s> s left): younger steps wait`, `backfill until … : younger steps that fit may start` or `no head priority: younger steps that fit may start`, plus `; bumped by position at HH:MM` or `; head priority recorded at HH:MM` when the head has a recorded bump; one `Cargo token: <session> <step>: waited <n> s (HH:MM-HH:MM)` line per matched wait, or `Cargo token waits: none`/`unknown`. All actor and session ids go into the one `roster.unit_labels_for` call. A failed bump read shows `unknown` and never becomes exit 2.
- `/build:queue` `head_line` adds `head for <n> min`/`head for: unknown`; `<n> bumps since it joined, most: <folder name> <step> (<count>)`/`no bumps since it joined`/`bumps: unknown`; the build-why window text; and `cargo token waited <n> s` (sum of the head step's waits)/`no cargo token wait`/`cargo token: unknown`. `Stalled:`, the `{"text", "tone"}` shape and the `QueueSnapshot` keys do not change.

**Files:**
- `scripts/build_queue/head_facts.py` — the reader, result types, backward scan (`BLOCK_BYTES`, `MAX_SCAN_BYTES`, `QUEUE_TIMES_PATH`, `BUMP_TABLE_ROWS`) and one formatter per fact
- `scripts/build_queue/build_why.py`, `build_queue.py` — the callers above
- `docs/as-built/build-memory-admission.md` — the build-why paragraph names the four facts and their sources
- `scripts/build_queue/test_head_facts.py`, `test_build_why.py`, `test_build_queue.py` — reader cases (block edges that split a bump row or a UTF-8 character), line order, label call, head-line forms

**Gotchas:**
- Bump scan: months from now back to the month of `since`, each file backward in 1 MiB blocks; only complete lines that match `"kind":"queue_bump"`; a torn last line is skipped. It stops at the first bump row with `at` before `since` (rows append in `at` order under the admission lock). A missing month file has no rows, not unknown. A read error, a malformed bump row (`at` without zone, a non-string name field, `old_position` not a positive int) or more than 32 MiB read makes the whole fact unknown; a partial count never shows. A bump row has no waiter key: the step is named only by `session`, `step`, `folder`, never as "this step".
- Token gaps match waiting steps only: same `step`, `token_left_at` >= the step's `since`, same non-empty `session_id`, else same `pid` and `start_time`. Duplicates count once. Other kinds in the file are skipped. The scan stops at the first line whose `at` is before the oldest waiting step's `since`.
- `Cargo token waits: none` only with a present record that has at least one valid line and no bad line. An absent file, no waiting step, a read error, a line without `at`, a torn line or an invalid `token_gap` (`pid` <= 0, `token_back_at` before `token_left_at` or after now) gives unknown; next to matched waits it adds a `Cargo token waits: unknown` line.

**Ruled out:** Waiting-table rows for a step in a token wait (it has no waiter row; the record holds only finished waits); a running step's waits; head time from `since` or `priority_since` (set only at priority or a bump, kept after a later bump moves the row back, deleted at a token give-back); SQLite for bumps (the index lags the live log); a second label read or the full roster.

### Phase 4f — A run counts as warm only when its target folder already built the same target with the same features  · status: done

#### As-built

- Rebuild bins: `none` (0 crates); `edited crate (1–3 crates)` and `cascade (4–49 crates)` for warm steps only; `first build here (1–49 crates)` (`FIRST_BUILD_LABEL`) for the rest; `cold (50+ crates)`. `rebuild_bin(crates_compiled: int, warm: bool) -> str` serves `rebuild_timings`, `package_rebuild_rows` (warm edited-crate nextest steps, via `KnownCrateStep.is_warm`) and `profile_step_durations`. The Rebuilds `Source:` line says edited-crate and cascade rows count only steps whose folder had built the same target with the same features before.
- `report.warm_step_clause() -> str` is the one warm rule, a SQL expression that `known_crate_steps` and `profile_step_durations` select. A step is warm when earlier finished steps (smaller `started_at_us`, `finished_s` not null) on the same `host`, `target_folder` and `build_base`, over all indexed history, together cover each item of its `build_items`. `--bins`, `--tests`, `--examples`, `--benches` cover their named members; `--all-targets` covers every item but `--doc`, and also covers `[]`; else only `[]` covers `[]`. A step with a null key is never warm.
- Step rows: `compiled_crate_names` from `parse.log_facts` (one name per counted line, in log order; length equals `crates_compiled`). `record.py` writes `target_dir` (`CARGO_TARGET_DIR` resolved against `cwd`, else `TARGET_DIR_UNSET = "unset"`) and `repo_identity` (one `memory_admit.git_identity(argv, cwd)` key, else `store.UNKNOWN_IDENTITY = "unknown"`), never null. `test_phase_fields` no longer copies the phase context's `repo_identity`; the sidecar keeps admission's value.
- Index `SCHEMA_VERSION = 25` rebuilds once from the source logs (4f set 24; the merge with builds' `test_build_key` step column set 25). 4f step columns: `compiled_crate_names`, `target_dir` and `repo_identity` (`TEXT NOT NULL`, `"unknown"` before 4f), `target_folder` (an absolute `target_dir`, else `worktree`, else `cwd`), `build_base`, `build_items`, `started_at_us` (UTC epoch microseconds). Index `steps_build_history (host, target_folder, build_base, started_at_us)`.
- `index.build_keys(argv: list[str]) -> CargoBuildKey | NonCargoBuild` is the one argv key function. `base` is sorted JSON of command (`nextest run` = `test`), profile, the sorted `--target` set, workspace, packages, excludes, features (split on commas and spaces), `all_features`, `no_default_features`. `items` is the sorted target-selector list; `[]` is cargo's default. Run options (`-E`, `--test-threads`, `--target-dir`, …) and words after `--` are dropped. A non-cargo step has null keys.

**Files:**
- `scripts/buildlog/parse.py`, `record.py`, `index.py`, `report.py` — the names above
- `scripts/buildlog/test_parse.py`, `test_index.py`, `test_report.py`, `test_test_phase_peak.py` — field values, hana argv keys, the warm rule and its three callers

**Binds later work:** `report.warm_step_clause()` and `rebuild_bin(crates_compiled, warm)` stay as they are. resources-unit's sweep (`scripts/lint/sweep.py`) reads step-row `target_dir` and `repo_identity`: the wire values `"unset"` and `"unknown"` and the names `record.TARGET_DIR_UNSET` and `store.UNKNOWN_IDENTITY` are its contract.

**Gotchas:**
- Never order step rows by the `started_at` text: the log mixes `.mmmZ`, `.ffffff+00:00` and `+00:00` endings. Use `started_at_us`.
- `index.py` must never import `record` again. Of the buildlog modules it imports only `store` and `parse`. builds' `scripts/lint/memory_admit.py` imports `index` at load, and `record.py` imports `memory_admit`, so an `index` → `record` import closes the cycle `record` → `memory_admit` → `index` → `record`.
- `record.py` imports `memory_admit`, so a change to `git_identity`, `RepositoryIdentity`, `UnknownRepository` or `test_threads` can break the recorder on every build step.
- `target_folder` is the recorded path. A symlinked `target/` or a `.cargo/config` `target-dir` reads as the worktree, so the step misses shared history and reads `first build here`. The two cases that can give a false warm are under "The package table's Source line says it counts only warm steps".
- History before 4f has `target_folder` = worktree, so a `CARGO_TARGET_DIR` folder reads `first build here` until it builds once with the new recorder.
- `index.py` writes `store.UNKNOWN_IDENTITY` for a missing `target_dir` too.
- The warm clause takes about 8 s over all steps with no host or kind filter; filter first. Each power-profile change in the last 7 days adds about 0.3 s to the report.

**Ruled out:** a copy of `git_identity` in the recorder; the phase context as a second `repo_identity` source; a reader for `compiled_crate_names` before a consumer exists.

### Phase 5 — The package table's Source line says it counts only warm steps  · status: done

#### As-built

- `rebuilds_section` prints the package table's `Source:` line as `f"Source: warm nextest steps that compiled {EDITED_CRATE_MIN_CRATES}–{EDITED_CRATE_MAX_CRATES} crates (the edited-crate row), by the first package(…) in the test filter; p50 and p95 are of compile time."`. The line names the steps that `package_rebuild_rows` counts: warm edited-crate nextest steps. No other report text changes.
- `docs/as-built/build-followups.md`: the Rebuilds **Layout** lists the five bins in `REBUILD_LABELS` order and quotes both `Source:` lines as `report.py` prints them. Its "Recorded build identity" paragraph gives the 4f step fields, the index columns and their limits.

**Files:**
- `scripts/buildlog/report.py` — the package table's `Source:` line in `rebuilds_section`
- `scripts/buildlog/test_report.py` — `test_nextest_rebuilds_show_packages_top_five_and_empty_cold_bin` checks that the line starts with `Source: warm nextest steps`
- `docs/as-built/build-followups.md` — the bins, the warm rule, both `Source:` lines, the recorded build identity
- `docs/as-built/buildlog-memory-stalls.md` — the index schema version

**Gotchas:** the warm rule can give a false warm in two known cases. A sweep that deletes a folder's output after a build writes no build-log row, so the next step in that folder can still read warm (comment at `report.warm_step_clause`). A `--target-dir` argument reads as the worktree, so a step that builds into another folder shares the worktree's history and can read warm.

### Phase 6 — A cut-short row no longer leaves bad JSON in the build log  · status: done

#### As-built

- `store.last_whole_row_end(handle: BinaryIO) -> int` returns the byte after the last `\n`, or 0 when the file has none. When the file ends in `\n`, it reads only that byte. Else it maps the file read-only and uses `rfind(b"\n") + 1`.
- `store.read_last_whole_row_end(path: Path) -> int` takes `LOCK_SH` and calls it. An absent file gives 0.
- `store.locked_append` takes `LOCK_EX` and cuts an unfinished tail with `os.ftruncate` to `last_whole_row_end` before the caller writes. A row counts as written only when its newline lands, so a whole JSON object with no newline is cut too. `record._resume_step_line` publishes a step row lost this way again; other rows stay lost. Readers see only whole rows and do not change.
- `record.step` saves `PendingStep.month_row_end` from `store.read_last_whole_row_end(month_file)`, not from `st_size`. `_resume_step_line` scans from it. `_read_pending_step` gives a record with only the legacy `month_offset` key a boundary of 0, so its rerun scans the month file from 0.
- On 2026-10-30 the live month files held no line that does not parse (1284 files read). No file changed and no backup exists.

**Files:**
- `scripts/buildlog/store.py` — `last_whole_row_end`, `read_last_whole_row_end`, the cut in `locked_append`, the module docstring
- `scripts/buildlog/record.py` — `PendingStep.month_row_end`, its capture in `record.step`, the legacy `month_offset` read in `_read_pending_step`
- `scripts/buildlog/test_store.py` — tail cut, short write and short read tests
- `scripts/buildlog/test_record_rerun.py` — rerun after a tail cut, tail-byte reads, legacy offset
- `docs/as-built/buildlog-memory-stalls.md` — `store.append_line` cuts an unfinished tail before it writes

**Binds later work:** a reader that keeps a month-file byte offset takes it with `store.read_last_whole_row_end(path)`. An `st_size` offset can sit inside an unfinished tail, and a later cut moves the file end below it. resources-unit's sweep offset (`TrimTake.log_offset`) is such a reader.

**Gotchas:**
- Every caller of `last_whole_row_end` holds the file's flock: exclusive in `locked_append`, shared in `read_last_whole_row_end`. Without it, a cut can shrink a mapped file.
- Never use a legacy `month_offset` as a boundary. It can sit inside a tail that a later cut removes.
- To rewrite a month file, truncate and write it in place under its flock. Never rename a new file over it: an appender that waits on the lock holds the old inode.
- Fix a bad line in a Mac host folder on the Mac. `sync.py` pulls those folders with `rsync -rt`, which replaces a changed file, so a natedev edit is lost at the next sync.

**Ruled out:** reader changes that skip bad lines (the writer keeps each file whole); a `\n` repair after an unfinished tail (the fragment became its own line that is not JSON); a second backward block reader beside `head_facts._backward_lines` (a short `os.read` skipped bytes, so the cut removed whole rows); a repair of the one-time removal script (it rewrote nothing and does not run again).

### Phase 7 — The build report shows how much memory running builds really hold, hour by hour  · status: done

#### As-built

- `cgroup_capture.py` is the one reader of slice files. It reads each slice's `memory.current` (`current_bytes`), `memory.swap.current` (`swap_current_bytes`), a `stat` record of named `memory.stat` lines (`anon`, `file`, `kernel`, `shmem`, `file_mapped`, `file_dirty`, `file_writeback`, `active_file`, `inactive_file`, `slab`, `sock`, `zswap`), `max_bytes`, `cpu.stat` (`usage_usec`, `user_usec`, `system_usec`) and raw `io.stat` lines into typed values. `sample` and `memory_snapshot` both call it. An unreadable file makes only its own field unavailable; a line the kernel does not print stays absent, never 0.
- Each 60 s sample row holds `samples.slices`, `filesystem` (free, total, used, identity; `disk.filesystem_usage` gives total as `f_blocks * f_frsize`) and `jobserver` (free pool tokens from `stevie -t` and `stevie -s /dev/steve-ci -t`, with a short timeout). The report reads memory, disk, pool and counter values only from this row. `memory_snapshots` stays for its existing readers. `index.SCHEMA_VERSION` is 26.
- `memory.current` is the one held total. Swap stays apart, `builds` and `ci` are never added together, and step `peak_mem_bytes` is never used for held memory.
- `BUILDLOG_SCOPE_SH` copies the scope's `cpu.stat` and `io.stat` by plain `cat` to `PEAK.cpu.stat` and `PEAK.io.stat`. `record.step` parses them into `scope_resources` in `PendingStep.row` before `_save_pending_step` and deletes them with the step's sources. A rerun takes the values from the pending record. A read failure other than a missing file keeps the sources and leaves the step pending for a rerun.
- `report` prints one Memory pressure line over the last 24 hours (ending now, or at the end of the local date for `report YYYY-MM-DD`): `Build memory held (60 s samples, last 24 h): average <x> GiB (`anon` <y> GiB, `file` <z> GiB, of which `inactive_file` <v> GiB) of the <cap> GiB cap; busiest hour <HH:00> at <w> GiB.` With no `current_bytes` reading it prints `Build memory held: no readings yet.` `cli.py usage-hours` prints one row per hour with average GiB held, processes, file cache, CPU cores busy, disk bytes read and written, lowest free disk and free pool tokens per pool, with a reading count for each value.
- `report.sample_counter_deltas` is the one counter pairing. It pairs readings by boot id and cgroup folder inode, and needs elapsed time above 0. A reset, a boot or cgroup change, or a gap over `report.MAX_SAMPLE_GAP_S` gives `unavailable`, never 0. Disk traffic covers only the top-layer device behind the build-target filesystem, found by mapping `st_dev` through `/sys/dev/block`; stacked layers are never summed and zram swap stays apart. Hours bucket by UTC hour and carry the local date, hour and offset, so the two 01:00 hours on 2026-11-01 stay two rows.

**Files:**
- `scripts/buildlog/cgroup_capture.py` — shared slice reader and parsers
- `scripts/buildlog/cgroup_test_support.py` — fake cgroup fixtures for tests
- `scripts/buildlog/sample.py`, `memory.py`, `disk.py` — sample, snapshot and filesystem rows
- `scripts/buildlog/record.py` — step counters from the scope's copied files
- `scripts/buildlog/report.py`, `cli.py`, `index.py` — held line, `usage-hours`, schema 26
- `scripts/lint/invoke.sh` — scope line that copies the counters
- `docs/as-built/buildlog-memory-stalls.md` — fields, line and command

**Gotchas:**
- `index.open_for_update` deletes an index of another schema version, so a manual run of a tree at a different schema sets `BUILDLOG_DIR` to a copy.
- `cli.py sample` also starts the Mac idle watcher.
- Per-step IO reads `unavailable` until nixos ee6d018 (`DefaultIOAccounting=yes`) is live at the next rebuild.
- `invoke.sh` runs the installed recorder, so a per-step counter change appears only after it lands on main.
- Hourly usage selects natedev through `natedev_step_predicate('samples')`; `store.host_name()` would empty the Mac's table.
- A day's pressure totals count an interval once, by its end. Only hourly usage splits an interval across hour edges, by covered seconds.
- Each component averages its own readings; a missing reading is skipped, and rows without `current_bytes` count as no reading.
- SQL `julianday` filtering and Python hour buckets can disagree at a window edge, so both use the same parsed instant.
- `shmem` is part of `file`, so the two are never added.
- Step CPU excludes compiles run by `sccache.service`; the slice totals count those compiles once.
- Free pool tokens are not CPU in use: each client also holds one implicit slot. steve is a CUSE device, and `FIONREAD` fails on it.

**Ruled out:**
- A Python capture inside the step scope: it raises the step's own peak, CPU and time.
- Summing `anon`, `file` and `inactive_file` as held memory: they overlap; only `memory.current` is held.
- Reading report values from `memory_snapshots.slices`: a failed append hides a saved reading.
- `IOAccounting` on `builds.slice` alone: it does not give scopes `io.stat`.

### Phase 8 — The queue shows true head times and cargo-token waits, and the report's recorder-error count holds only real errors  · status: done

#### As-built

- **Head time.** `memory_admit.write_head_since` marks the first waiter in `ordered_waiters` (the one queue order that reads and head marking share). It removes `head_since` from every other row first. The head keeps the value it holds, or gets the transaction's `now`. Thus a row that loses the head and gets it back starts a new time. `check()` and `admit_head()` call it under the admission lock. In `check()` it runs after an admission removes the head's file, so the next head is marked in the same transaction. `check()` copies `head_since` from `previous` when it rebuilds its waiter dict. A bump does not write a head time: `bump_waiter` raises `BumpOff`, and only stored order fields (such as `bump_order`) change queue order.
- **Cargo-token gaps.** `buildlog_release_memory` in `memory_gate.sh` sets `BUILDLOG_MEM_TOKEN_LEFT_AT`. The next `buildlog_wait_for_memory` with `BUILDLOG_MEM_WAIT_SINCE` set records `BUILDLOG_MEM_TOKEN_BACK_AT`. It passes both times to its first `check` call only, as `BUILDLOG_TOKEN_LEFT_AT` and `BUILDLOG_TOKEN_BACK_AT`, and then unsets them. `memory_admit.append_token_gap` appends one `{"kind": "token_gap", "at", "session_id", "step", "pid", "start_time", "token_left_at", "token_back_at"}` line to `<buildlog root>/admission/queue_times.jsonl` through `store.append_line`. On each append it drops lines older than `QUEUE_TIMES_DAYS` (7). When no times are given, no line is written.
- **Recorder writes stay out of admission.** The `recorder_write(context)` context manager wraps the head-time write, the token-gap write and the token-time read. A failure goes to `store.note_error(context)` and never changes the admission result. Invalid token times (not finite, not ordered, or later than `now`) are noted as one error and give no line.
- **Bump text.** `head_facts.bump_text` prints "1 bump" for one and "bumps" for more.
- **Health line.** `report.recorder_health_lines` gives the action that fits each count: errors give `see <ERRORS_DISPLAY_PATH> on <machine>`, and unfinished records (or a stale or missing snapshot) give `run buildlog launches on <machine>`. With both counts, the line shows both actions, joined by `; `.
- **No-leak test.** `scripts/buildlog/test_log_isolation.py` runs each launch-recording test module in a subprocess with `HOME` at a temporary folder and `BUILDLOG_DIR` unset. It asserts that no `errors.log` appears.

**Files:**
- `scripts/lint/memory_admit.py` — `recorder_write`, `token_times_from_environment`, `ordered_waiters`, `write_head_since`, `append_token_gap`, `QUEUE_TIMES_PATH`/`QUEUE_TIMES_DAYS`, `BumpOff`
- `scripts/lint/memory_gate.sh` — token release time and token-back time, passed to the first re-check only
- `scripts/build_queue/head_facts.py` — singular and plural bump text
- `scripts/buildlog/report.py` — health line actions for each count
- `scripts/buildlog/test_log_isolation.py` — no-leak regression test
- `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_head_facts.py`, `scripts/buildlog/test_report.py`, `scripts/buildlog/test_record_rerun.py` — tests for head time, token gaps (shell protocol included), bump text and health actions
- `docs/as-built/build-memory-admission.md`, `docs/as-built/buildlog-memory-stalls.md` — head-time and token-gap writers; health line actions

**Gotchas:**
- The readers stay as they were: `head_since` counts only when finite, >= 0 and <= now, and a token gap matches only a waiting step with the same session, step, pid and start time.
- A recorder failure is visible only as an `errors.log` entry. The admission result does not show it.
- Old `errors.log` entries can come from older copies of a test module in other worktrees. This tree isolates its launch-recording tests through the shared `BuildLogIsolatedTest`.

**Ruled out:** a head-time write at bump time (bump is off; order changes only through stored order fields); a change to `verify.sh` (the gate script carries both token times).

### Phase 9 — The build-log modules import without a cycle  · status: done

**Depends on:** Phase 8

#### As-built

`FilesystemUsage` and `filesystem_usage(path)` live only in `scripts/buildlog/filesystem.py`, which imports `cgroup_capture` and neither `sweep` nor `record`. `disk.py` and `sample.py` both import `filesystem` at module level, so `sample` no longer imports `disk`, and the cycle `disk > lint/sweep > record > sample > disk` that basedpyright reported on the builds merge is gone. No re-export remains in `disk.py`; output is unchanged.

**Files:**
- `scripts/buildlog/filesystem.py` — the one filesystem capacity and device reader.
- `scripts/buildlog/disk.py`, `scripts/buildlog/sample.py` — import it there.
- `scripts/buildlog/test_disk.py` — patches and fixtures name `filesystem`.
- `scripts/buildlog/test_sample.py` — `test_filesystem_reading_does_not_import_disk_or_sweep` reads a sample in a fresh process and requires that neither `disk` nor `sweep` is imported.

**Gotchas:** the cycle exists only on a tree that also holds the builds `sweep > record` edge, so basedpyright on this branch alone cannot see it; check import cycles on a trial merge with builds. The build-log tests need `PYTHONPATH=scripts/buildlog` when run as `python3 -m unittest scripts.buildlog.…`.
