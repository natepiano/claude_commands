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

**Depends on:** none

#### Work Order

**Goal:** after any failed append, the build log's month file holds only whole rows, so every reader parses each line.

**Spec:**
- Defect (showrunner, from resources' Phase 4 review, 2026-10-30): `store._write_all` writes a row in a loop of `os.write` calls. When one write is short and the next fails (a full disk, for example), part of the row stays in the file with no newline. Since 4b, `locked_append` then writes a `\n` after that part, so the part becomes a line of its own that is not JSON; readers such as `sweep.read_rebuild_steps` (`scripts/lint/sweep.py:2207`, resources-unit's file) fail on it: it calls `json.loads` on each newline-terminated line and does not catch `ValueError`. Resources' uncommitted work renames it `read_rebuild_evidence`, which reads through `sweep.build_log_rows` and behaves the same way.
- Showrunner's ruling: under the append lock, before it writes, the appender cuts an unfinished tail back to the last newline, then writes the row.
- In `store.locked_append`, replace the `\n` repair with that cut: read only the last byte when it is `\n` (the usual case); otherwise find the last `\n` by reading back from the end, and `os.ftruncate` the file to just after it, or to 0 when the file has no `\n`. The cut runs under the same `flock`, before the caller's write.
- `record.step` takes `month_offset` from the same `store` function instead of `st_size` (Constraints).
- A row counts as written only when its newline lands, so a whole JSON object with no newline after it is cut too. A step row lost this way is published again by 4b's recovery (`record._resume_step_line`), which scans for its id; other rows stay lost, as they were before.
- No reader changes. Old damage is removed once instead (showrunner, 2026-10-30): count the lines that do not parse in each live month file under `~/.local/state/buildlog/`. In each file a reader still reads that has any, remove those lines once, after a backup copy beside the file named so that no reader's glob matches it. The month-file globs after the phase-start merge are `index.py:844` and `record.py:1007` (`*/*.jsonl`), `ci.py:135` (`ci/*.jsonl`) and `scripts/lint/sweep.py:2209` (`natedev/????-??.jsonl`); a name that continues after `.jsonl` matches none of them. Run `git grep -n 'glob(' -- scripts` again before the run. Rewrite the file in place under its `flock` (truncate, then write the kept lines; same inode): never rename a new file over it, because an appender waiting on the lock holds the old inode. Skip and report a file that a pending 4b step record names (its `month_offset` would move): the records are `record.pending_steps_root()/*.json`, and each names its month file through its row's `host` and `started_at` (`record._step_month_file`). The index reads each rewritten file again through its existing path: `index.update()` calls `read_file` (`index.py:784-795`), which forgets a file and reads it from 0 when the file is smaller than its stored offset or the bytes before that offset (`marks`) changed. Name that path in the output. `sync.py` pulls the Mac's host folders with `rsync -rt` (`sync.py:235-246`), which replaces a changed file by rename, so the next sync brings the Mac's bad lines back. Report each bad file in a Mac host folder and leave it unchanged. This is a one-time live step, not new repository code; save its output (each file, lines removed, backup path) to the phase folder for the notice.
- Update the module docstring (`store.py` lines 9–10). No as-built doc names the `\n` repair; `docs/as-built/buildlog-memory-stalls.md:46` says `store.append_line` "writes one line under a flock with `O_APPEND`": add there that it first cuts an unfinished tail back to the last newline. Take every name from the code.
- The checkpoint notice names resources-unit as also-affected: the cut can move the file end below a size offset taken over an unfinished tail. Their uncommitted `TrimTake.log_offset` (`scripts/lint/sweep.py:1081` in their worktree) is such an offset, and `build_log_rows` (line 1133 there) then raises `ValueError` on a row fragment. Offer them the one `store` function from Constraints.
- The director corrected `docs/as-built/build-targets.md:127` (`rebuild_bin(crates_compiled, warm)`, with `warm` from `warm_step_clause()`), which 4f made wrong; this phase's checkpoint commits it. The phase-start merge of builds at 31eaac9c kept both new step columns (`started_at_us`, `test_build_key`) and set `SCHEMA_VERSION = 25`; that merge commit carries the schema lines in `docs/as-built/build-memory-admission.md:168` and `docs/as-built/buildlog-memory-stalls.md:17`.

**Files:**
- `scripts/buildlog/store.py` — the end-of-last-whole-row function, the cut in `locked_append`, the module docstring
- `scripts/buildlog/record.py` — `month_offset` from that function (line 849)
- `scripts/buildlog/test_store.py` — the new test and the changed assertion
- `scripts/buildlog/test_record_rerun.py` — the offset rerun test
- `docs/as-built/buildlog-memory-stalls.md` — line 46, the cut
- `docs/as-built/build-memory-admission.md`, `docs/as-built/build-targets.md` — director edits, already in the tree

**Workers:** 1 writer + 1 tester
- `impl` — `store.py`, `record.py`'s `month_offset` capture, `docs/as-built/buildlog-memory-stalls.md`, and the one-time live removal after its code passes
- `test` — `test_store.py`: a test that cuts a write short: `os.write` writes a few bytes of a row, then raises `OSError`; `append_line` raises; the next `append_line` leaves the file as the old rows plus the new row, and every line parses as JSON. Also a file with no newline at all is cut to empty. It must fail on the old code. And `test_record_rerun.py`: the month file ends in an unfinished tail when `record.step` starts; the row write lands, then the save fails (for example `os.fsync` raises); the rerun leaves the row in the file once. It must fail with the cut in place and `month_offset` still from `st_size`.

**Assertion-Change:** `scripts/buildlog/test_store.py` `test_append_separates_an_interrupted_tail_before_the_next_row` — old: `self.assertEqual(path.read_bytes(), tail + b'\n{"id":"next"}\n')` for both tails; new: the file is `b'{"id":"next"}\n'` for both tails, because the ruling cuts every unfinished tail. Rename the test for the new behavior. `test_append_reads_only_the_tail_byte_and_keeps_the_lock_through_fsync` stays as it is: its file ends in `\n`, so the cut still reads one byte with `os.read(fd, 1)`. The test patches `os.read` and asserts `read_sizes == [1]`.

**Assertion-Change (director ruling after review pass 1, 2026-10-30):** `scripts/buildlog/test_record_rerun.py`:
- `test_normal_step_appends_without_opening_month_history_for_reading` becomes `test_normal_step_reads_only_tail_bytes_without_scanning_month_history` — old: any read open of the month file raises `AssertionError("a normal step read the monthly history")`; new: the only month-file reads are `os.read` of size 1 at `original_size - 1`, `read_sizes == [1, 1]`, because `record.step` now reads the tail byte to find the row end, and the cut reads it again.
- `assert_short_append_recovers` (used by `test_fragment_then_normal_step_and_call_recovers_one_physical_row_per_id`, `test_complete_object_without_newline_then_other_writers_recovers_without_duplicate` and the two stop tests) — old: the stop is the `\n` write (`self.assertEqual(bytes(data), b"\n")`), then `self.assertTrue(month.read_bytes().endswith(b"\n"))`, and `self.assertEqual(finished.added, not complete_object)`; new: the stop is the cut (`os.ftruncate`, `self.assertEqual(length, 0)`), then `self.assertEqual(month.read_bytes(), b"")`, and `self.assertTrue(finished.added)`, because a whole object with no newline is cut and its row is published again.
- `test_stop_after_fragment_repair_newline_recovers_without_losing_later_rows` and `test_stop_after_complete_object_repair_newline_recovers_without_duplicate` become `test_stop_after_fragment_tail_cut_recovers_without_losing_later_rows` and `test_stop_after_complete_object_tail_cut_recovers_without_duplicate`.

**Repair round 1 (director ruling, 2026-10-30):** a short `os.read` could make the row-end search skip bytes, so the cut removed whole rows. `store.last_whole_row_end` now reads the last byte, and when it is not `\n` maps the file read-only and finds the last `\n`, with no block loop. Its caller holds the file's flock, so a cut cannot shrink the mapping: `locked_append` holds the exclusive lock, and `record.step` captures through `store.read_last_whole_row_end`, which takes a shared one. A pending record written before this phase can hold an offset inside an unfinished tail, so the row end is saved under a new key, `month_row_end`, and a record with only `month_offset` scans its month file from 0.

**Assertion-Change (director ruling after repair round 1, 2026-10-30):** `scripts/buildlog/test_record_rerun.py` `test_recovery_starts_at_the_saved_month_offset_and_keeps_other_appends` — old: `self.assertEqual(saved["month_offset"], len(prefix))`; new: `self.assertEqual(saved["month_row_end"], len(prefix))`, the same value under the renamed key.

**State:** the month file's tail: a cut under the exclusive lock, before the write. A pending step's `month_row_end`: the end of the last whole row at record time; a cut never moves below it. A record with only the old `month_offset` key scans from 0. A rerun after a failed append cuts the same tail again and writes once. The one-time removal: a second run finds no bad line and changes nothing, and never overwrites an existing backup.

**Constraints from prior phases:** 4b's recovery scan (`record._resume_step_line`, `record.py:682-697`) seeks to `month_offset` under `locked_append` and skips lines that do not parse. Today `record.step` sets `month_offset` to the month file's `st_size` (`record.py:849`), outside the month file's lock, so it can sit inside an unfinished tail. The cut would then put the next row before `month_offset`, and a rerun after that row landed would miss it and append it again. So `month_offset` must be the end of the last whole row: the byte after the last `\n` at or before the size, or 0. A cut removes only bytes after the last `\n`, so bytes up to that point never change. The capture still takes a shared flock, because the search maps the file (repair round 1). One `store` function finds the end of the last whole row; the cut and the `month_offset` capture both call it. No second backward read in `record.py`, and no copy of `head_facts._backward_lines`' block loop (`scripts/build_queue/head_facts.py:111`).

**Acceptance gate:** `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY PYTHONPATH=scripts/buildlog python3 -m unittest scripts.buildlog.test_store scripts.buildlog.test_record scripts.buildlog.test_record_rerun scripts.buildlog.test_recorder_health` from the worktree root green, with the new `test_store` tests failing on the old code and the offset test failing on the cut with `st_size` capture; basedpyright on `scripts/buildlog/store.py scripts/buildlog/record.py scripts/buildlog/test_store.py scripts/buildlog/test_record_rerun.py` `0 errors, 0 warnings, 0 notes`; live: the one-time removal above ran, with a backup per changed file, and a second count finds no bad line in any file a reader reads; `python3 scripts/buildlog/cli.py report` from the worktree root runs clean.

### Phase 7 — The build report shows how much memory running builds really hold, hour by hour  · status: todo

**Depends on:** none

#### Work Order

**Goal:** `/build:report` gives the true average memory that running builds held in each hour of the last 24 hours, from 60 s readings of current use, not from step peaks.

**Spec:**
- User, 2026-10-30: asked for the average real memory use of running builds per hour. The only answer the log could give summed step peaks, and one hour came to 56 GiB, above the 44 GiB cap of `builds.slice`. The 60 s `memory_snapshot` rows hold only `memory.peak` (the peak since the slice started), so no true reading exists.
- Accuracy first (user, 2026-10-30: "we keep making mistakes - undercounting, double counting"). In `memory.slice_state` (`scripts/buildlog/memory.py`), store each reading as the kernel gives it, under its own name, and derive nothing at write time:
  - `current_bytes` from `memory.current`: the slice's whole charge, the same number `memory.max` enforces. It already includes every child cgroup (each step's scope) and file cache and kernel memory. This is the one "held" total; never add child cgroups, `memory.stat` lines or swap to it.
  - `swap_current_bytes` from `memory.swap.current`; kept apart, never added to `current_bytes`.
  - From `memory.stat`, these lines as named fields in a `stat` record: `anon`, `file`, `kernel`, `shmem`, `file_mapped`, `file_dirty`, `file_writeback`, `active_file`, `inactive_file`, `slab`, `sock`. `shmem` is part of `file`, so never add the two; `inactive_file` is the cache the kernel frees first. A line the kernel does not print is stored as absent, never 0.
  - `max_bytes` at that instant (already read as `max`); `builds` and `ci` slices stay separate, never summed.
  Use the existing typed slice record and its one reader of slice files; add no second reader.
- In the report's Memory pressure section, add one line from the samples of the last 24 hours: `Build memory held (60 s samples, last 24 h): average <x> GiB (`anon` <y> GiB, `file` <z> GiB, of which `inactive_file` <v> GiB) of the <cap> GiB cap; busiest hour <HH:00> at <w> GiB.` The hour uses the user's zone, as other report times do. With no sample that has `current_bytes`, the line reads `Build memory held: no readings yet.`
- Add a command that prints the hour-by-hour table on demand, in the existing `cli.py` (columns: hour, average GiB held, processes, file cache, samples). Times in the user's zone.
- Averages are the mean of the readings in each hour, with the reading count shown; a missing reading is skipped, never 0. Rows written before this phase have no `current_bytes`; they count as no reading. Step `peak_mem_bytes` is never used for this line.
- Update `docs/as-built/buildlog-memory-stalls.md` (the snapshot fields, the new line and the command).
- CPU and disk, same rules (user, 2026-10-30: record what builds really do, for trends). Store raw kernel counters as read; derive rates only in the report, from the difference of two readings; a missing file is absent, never 0:
  - per step, at step end, from the step's own scope cgroup (the folder `BUILDLOG_SCOPE_SH` writes to `"$BUILDLOG_PEAK.cgroup"`): `cpu.stat` `usage_usec`, `user_usec`, `system_usec` and `io.stat` `rbytes`, `wbytes` summed over devices (the sum, and the per-device lines kept as read);
  - per 60 s sample, for the builds slice and the CI slice apart: `cpu.stat` `usage_usec` and `io.stat` `rbytes`, `wbytes`;
  - per 60 s sample: free and total bytes of the filesystem that holds the build targets (`os.statvfs`); `disk.json` keeps only the latest snapshot, so it gives no trend;
  - per 60 s sample: free slots in each jobserver pool (`/dev/steve`, 31; `/dev/steve-ci`, 14), read without taking a slot (for example `FIONREAD` on the fifo). If no read is safe, record nothing and say so in the as-built; never take and return a slot to count.
  - Report: the hourly command shows, per hour, average CPU cores busy for builds (`usage_usec` difference / wall time), disk bytes read and written, lowest free disk and average free slots per pool, each with its reading count.

**Files:**
- `scripts/buildlog/memory.py` — the new slice fields
- `scripts/buildlog/report.py` — the new line
- `scripts/buildlog/cli.py` — the hourly command
- `scripts/buildlog/test_sample.py` — slice field tests
- `scripts/buildlog/test_report.py` — line tests
- `docs/as-built/buildlog-memory-stalls.md` — the fields, line and command

**Workers:** 1 writer + 1 tester
- `impl` — `memory.py`, `report.py`, `cli.py`, the as-built doc
- `test` — `test_sample.py`: the fields come from a fake slice folder; `test_report.py`: the line with readings, with only old rows (`no readings yet`), and an average that never exceeds the cap for a fixture within it

**State:** none.

**Constraints from prior phases:** samples reach `index.sqlite` table `memory_snapshots` (`slices` JSON); read them from there, through the existing query path.

**Acceptance gate:** `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY PYTHONPATH=scripts/buildlog python3 -m unittest scripts.buildlog.test_sample scripts.buildlog.test_report` from the worktree root green; basedpyright on the five Python files `0 errors, 0 warnings, 0 notes`; live cross-check: read `memory.current` and `memory.stat` by hand in the folder `memory.builds_path()` returns at one instant beside one sample run, and show that the stored values equal the files, that `anon + file + kernel` is within 5% of `current_bytes` (report the gap), and that no stored `current_bytes` exceeds `max_bytes`; live: after the installed sampler writes a row with `current_bytes` (or one `python3 scripts/buildlog/cli.py` sample run from the worktree), `python3 scripts/buildlog/cli.py report` prints the new line and the hourly command prints a row per hour with readings.
