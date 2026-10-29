# builds-report

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `/build:queue` names each step's showrunner, unit director and session, shows the bare step name in both tables, and gives free memory its own column.

> **As-built disposition: amend** — `docs/as-built/build-memory-admission.md` (Queue API and Queue order bullets); `docs/as-built/buildlog-memory-stalls.md` (machine samples and the report section)

> **Production: builds** — unit `report-unit`; production doc `docs/plans/builds-production.md`

## Source

2026-10-29 10:07 EDT

Own what the build log records and what its reports show: the build log, /build:report, /build:mac_report, and the /build:queue and /build:status tables. The user has build report changes coming; they come to this unit. Until they arrive, stand by.

2026-10-29 10:35 EDT (the user, after a `/build:queue` run, with three screenshots of its tables)

[Image #1] what is the 'clone88ci' session - i have no way to know or track it - we should have the build report show the showrunner, the unit director and the session - at least - so i can identify

[Image #2] the steps are output differently in the two tables - they need to be just the short version - nextest, clippy, as in the running

[Image #3] needs, free, should be their own columns

2026-10-29 (the showrunner's request after phase 1, relayed for the builds production)

Add to `buildlog sample` and its samples table the /proc/pressure/cpu and /proc/pressure/io totals (some, and full where present), and the /proc/stat idle and iowait counters. Show them in /build:report's Memory pressure section, or in a section next to it, as share of time. Tests fake every reading, as in phase 1. Reason: the build log records memory only, so history cannot say whether natedev was CPU or disk bound.

2026-10-29 14:00 EDT (the user, relayed by the showrunner; it replaces the user's earlier "builds-report or hana-lfo" message)

i don't mind columns in reports showing a showerunner and a unit as long as they are using the short name - it's the file path that iwas discusing as being hyphenated - both situations should use the short names

The showrunner: keep the separate Showrunner and Unit columns, and fill each with the short name the user uses: Showrunner "builds" or "hana", Unit "report" or "lfo", never "report-unit" or a long session name. Only file paths join them with a hyphen (builds-report). Take the name from the one place that names sessions, never a second copy of that logic. Put it in Phase 2 if it fits, or in a small phase right after.

## Delegation Context

- **Project:** `~/.claude` — the build system scripts; this plan changes the `/build:queue` tables.
- **Project started:** 2026-10-29T14:37:53.345+00:00
- **Stack:** Python 3.13, stdlib only; `unittest`; basedpyright with zero errors and zero warnings, no file-level ignores, no `Any`.
- **Layout:**
  - `scripts/build_queue/` — the `/build:queue` command script and its tests
  - `scripts/lint/memory_admit.py` — the memory gate; it writes the waiter rows and builds the `QueueView` that `build_queue.py` reads
  - `scripts/message/roster.py` — the session roster: each live Claude session and Codex worker with its showrunner, unit and director
  - `commands/build/queue.md` — the `/build:queue` command text
  - `docs/as-built/build-memory-admission.md` — the as-built doc for admission and the queue
- **Key files:**
  - `scripts/build_queue/build_queue.py` — `render_queue` (both tables, line ~84), `reason_text` (wait reasons, line ~40), `main` (`bump`)
  - `scripts/build_queue/test_build_queue.py` — table tests; its `setUp` fakes `BUILDLOG_DIR`, `BUILDLOG_PROC`, `BUILDLOG_MEMINFO`, `BUILDLOG_MEMORY_PRESSURE`, `NOTIFIER_SESSIONS_DIR` and `USER_ZONE`
  - `scripts/lint/memory_admit.py` — `waiter_identity` (line ~865), `QueueIdentityReader.identity` (line ~894), `read_queue` (line ~929), `head_wait_text` (line ~262), `bump_waiter` (line ~1010), `WaitingStep` / `RunningStep` (line ~285)
  - `scripts/lint/test_memory_admit.py` — memory gate tests, including `head_wait_text` and queue view cases
  - `scripts/message/roster.py` — `roster(environment, ThisMachine())` (line ~1386) returns `Roster(entries, problems)`; `RosterEntry` (line ~202) with `kind`, `role`, `name`, `session: ClaudeSession | NoClaudeSession`, `thread: CodexThread | NoCodexThread`, `showrunner: RunsProduction | UnderShowrunner | ProductionWithoutRunningShowrunner | NoShowrunner`, `director: DirectsUnit | UnderDirector | NoDirector` (classes at lines ~69–170)
- **Test lanes:** none — tests sit beside the code (`scripts/build_queue/test_build_queue.py`, `scripts/lint/test_memory_admit.py`).
- **Build:** none (Python).
- **Test:** from the worktree root, `python3 -m unittest scripts.build_queue.test_build_queue scripts.lint.test_memory_admit`; the merge gate runs the full suite.
- **Lint:** `basedpyright scripts/build_queue scripts/lint/memory_admit.py scripts/lint/test_memory_admit.py` — pass is `0 errors, 0 warnings`.
- **Invariants:**
  - Every test fakes every live machine reading the queue view takes: meminfo, memory pressure, `/proc`, the sessions folder, and now the roster. No test reads the live roster (production doc, resources-unit's first work: a test that reads live machine state waits on the machine's load).
  - `head_wait_text` keeps its current text: `verify.sh`'s memory wait lines use it (`memory_admit.py` line ~1124). The queue table gets its own reason text.
  - `memory_admit.py` is resources-unit's file (production doc, Hub files: report-unit touches the queue view). The checkpoint notice names that owner.

## Phases

### Phase 1 — Queue tables name who runs each step  · status: done

#### As-built

- `/build:queue` prints two tables with identity columns before `Session`:
  - Running: `# | Showrunner | Unit director | Session | Step | Folder | Promised | Using | Delta | State`
  - Waiting: `# | Showrunner | Unit director | Session | Step | Folder | Waiting since | Needs | Free | Reason`
- `build_queue.py` reads `roster.roster(os.environ, roster.ThisMachine())` in a worker thread while `memory_admit.queue_view()` runs on the main thread (its lock timeout uses a signal). `session_labels` maps each roster entry to its labels: Session is `entry.name`; Showrunner is `UnderShowrunner.name`, or `entry.name` for `RunsProduction`; Unit director is `UnderDirector.name`, or `entry.name` for `DirectsUnit`; else `—`. A step matches by `session_id` (Claude) or `thread_id` (Codex). An unmatched step keeps its recorded session name, else the folder basename, with `—` in both new columns.
- When the roster read raises or returns problems, the tables still print, and one line after Totals says `Roster: <first problem>; Showrunner and Unit director show — for unmatched steps`.
- `waiter_identity(step, worktree)` stores the bare step, so the Step column is one word (`nextest`, `clippy`), and the `queue_bump` build-log row records the bare step. `BehindHead` carries `head_session_id`; `behind <label> (waiting N min)` uses the head's Session label, and `head_name` is the label for an unmatched head.
- The Free column shows `view.available` in GiB, or `—` when any waiter has `ReadingUnavailable`. Reason text omits the amounts in Needs and Free. `NeedsMemory` carries `need`, `free`, `threshold`, `growth`, `reserve` and `head_need`, and its Reason names the threshold and its parts: `needs <threshold> GiB free: this step <need>, running steps may grow <growth>, head <head_need>, earlyoom <reserve>` (a part that rounds to 0.0 is left out). `build_queue.py` builds the head Reason from the `HeadAdmissionStatus` fields; `head_wait_text` is unchanged.
- `bump <selector>` also takes a shown Session label. A label that matches one waiting row resolves to that row's position; a label on two rows prints the ambiguous error and moves nothing.

**Files:**
- `scripts/build_queue/build_queue.py` — roster read, `session_labels`, identity and Free columns, Reason text, bump by Session label
- `scripts/lint/memory_admit.py` — bare waiter step; `BehindHead.head_session_id`; `NeedsMemory` threshold parts
- `scripts/build_queue/test_build_queue.py`, `scripts/lint/test_memory_admit.py` — tests with a faked roster (no test reads the live roster)
- `commands/build/queue.md` — the columns, the roster source, the bare step, the Session-label selector
- `docs/as-built/build-memory-admission.md` — Queue API and Queue order bullets

**Binds later work:** `session_labels` lives in `build_queue.py`; the phase that moves it into `roster.py` changes its tests on purpose.

**Gotchas:**
- Waiter rows written by an older installed copy of `memory_admit.py` still store `"<repo> <step>"`, so Waiting shows prefixed steps until this branch is the installed copy.
- The checkpoint assertion guard refuses `::*` wildcards: each changed test expectation needs its own `Assertion-Change: <path>::<test> — <why>` line.

**Ruled out:** a read-side strip of the `<repository_name> ` prefix from waiter steps — the write-side fix is enough once installed.

### Phase 2 — Samples record CPU and disk pressure  · status: todo

**Depends on:** Phase 1

#### Work Order

**Goal:** each 60 s machine sample records CPU and IO stall totals and the CPU idle and iowait counters, and `/build:report` shows them as share of time beside Memory pressure, so the build history says whether natedev was CPU bound, disk bound or neither.

**Spec:**

1. **New readings in `buildlog sample`** (`scripts/buildlog/sample.py`, `sample()` line ~150). Add module paths beside `PRESSURE`: `CPU_PRESSURE = Path("/proc/pressure/cpu")`, `IO_PRESSURE = Path("/proc/pressure/io")`, `STAT = Path("/proc/stat")`. Read each once per sample:
   - CPU and IO PSI: `parse_pressure(text, require_both=False)`. `some` and `full` totals in microseconds. The kernel may omit the `full` line for CPU; that value is `None`.
   - `/proc/stat`: the first line that starts with `cpu ` (all CPUs). Record field 4 `idle` and field 5 `iowait` (USER_HZ ticks since boot), and the sum of fields 1–8 (user, nice, system, idle, iowait, irq, softirq, steal) as the total. Guest time is already inside user and nice, so it is not added. Add a parser `parse_cpu_ticks(text) -> CpuTicks` (NamedTuple: `idle`, `iowait`, `total`) beside `parse_meminfo`; it raises `ValueError` on a missing or short `cpu ` line.
   - A missing file or a parse error makes only that reading `None`. The memory readings stay required (the Linux-only check at line ~150 does not change): a sample with no CPU or IO reading is still written.
   - Record fields: `cpu_stall_some_us`, `cpu_stall_full_us`, `io_stall_some_us`, `io_stall_full_us`, `cpu_idle_ticks`, `cpu_iowait_ticks`, `cpu_total_ticks`.

   **Director's call:** the total ticks are recorded too. A share of time needs a denominator; wall time times CPU count goes wrong when CPUs go offline, the tick total does not. One field; a one-line revert if not wanted.

2. **Samples table** (`scripts/buildlog/index.py` `SAMPLE_COLUMNS`, line ~245). Add the seven columns as `INTEGER`, NULL when not recorded, each with a one-line description in the existing style (`"machine CPU some stall counter since boot, microseconds; NULL when not recorded"`, `"CPU idle ticks since boot from /proc/stat, USER_HZ"`, and so on). Raise `SCHEMA_VERSION` (22 → 23) so the index rebuilds from the JSONL files, the way earlier sample columns were added. Older sample rows load with NULL in the new columns.

3. **Shared counter deltas** (`scripts/buildlog/report.py`). `memory_pressure_section` sums counter deltas between samples inside its loop (line ~846): same host, gap at most `MAX_SAMPLE_GAP_S`, same boot id (a new boot counts the counter from zero). Move that rule into one function that the memory stall and the new section both use, so the boot and gap rules live in one place. A pair where either value is NULL adds nothing to that counter, and adds nothing to its time denominator. The memory stall line keeps its current text and numbers.

4. **New report section** `### CPU and disk pressure`, right after `### Memory pressure` (line ~1796), from the same day's samples. Per host that has at least one counted pair, one line:

   `natedev: CPU some 41.2%, full —; IO some 3.1%, full 1.0%; CPU idle 52.0%, iowait 0.4% (of 14 h 20 min sampled)`

   - PSI share = summed stall delta in microseconds / summed wall-time delta of the same counted pairs in microseconds.
   - Idle and iowait share = summed tick delta / summed total-tick delta of the same pairs.
   - A value with no counted pair shows `—`. A host with no counted pair for any of the seven is left out. When no host has one, the section says `CPU and IO samples: none.`
   - One decimal place, like the report's other percentages. The `(of …)` part is the sampled wall time counted for the PSI shares, written the way the report writes other durations.
   - The section's Source line: `Source: 60 s machine samples (/proc/pressure/cpu, /proc/pressure/io, /proc/stat).`

5. **Docs.** `commands/build/report.md`: name the new section in one sentence. `docs/as-built/buildlog-memory-stalls.md`: the sample record's new fields and the new section.

**Files:**
- `scripts/buildlog/sample.py` — paths, `parse_cpu_ticks`, the seven record fields
- `scripts/buildlog/index.py` — seven `SAMPLE_COLUMNS`, `SCHEMA_VERSION` 23
- `scripts/buildlog/report.py` — shared counter delta function, the new section
- `scripts/buildlog/test_sample.py`, `scripts/buildlog/test_report.py`, `scripts/buildlog/test_index.py`
- `commands/build/report.md`, `docs/as-built/buildlog-memory-stalls.md`

**Workers:** `2 writers` — the open phase 1 workers, `test` still opened as impl.
- `impl` — `sample.py`, `index.py`, `report.py`, `commands/build/report.md`, `docs/as-built/buildlog-memory-stalls.md`
- `test` — `test_sample.py`, `test_report.py`, `test_index.py`. Fake every reading with `patch.object(sample, …)` on the module paths, as `test_sample.py` fakes `MEMINFO` and `PRESSURE` now; never read live `/proc`. Cases: all readings present; CPU without a `full` line; a missing CPU, IO or stat file (sample still written, that field `None`); a malformed `/proc/stat`; the report shares for two hosts with known deltas; a reboot between samples; a gap over `MAX_SAMPLE_GAP_S`; old rows with NULL columns next to new rows; no samples; the memory stall line unchanged.

**Constraints from prior phases:** Phase 1 changed only `/build:queue` (`scripts/build_queue/`, `memory_admit.py`); no file here overlaps it. The checkpoint's assertion guard refuses `::*` wildcards: every changed or removed test expectation needs its own `Assertion-Change: <path>::<test> — <why>` line, which the test worker lists in its summary.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` green, and the production Merge tests (`env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only`) green.
- `basedpyright scripts/buildlog`: `0 errors, 0 warnings`.
- Live check on natedev from the worktree: run the sampler once with `BUILDLOG_DIR` set to a scratch folder and read the JSONL line: all seven fields hold integers (`cpu_stall_full_us` may be `null` if the kernel omits it). Then build a report from a scratch log with two such samples a minute apart and see the new section with shares between 0% and 100%.
- Speed: one sample reads three more small `/proc` files. Time 20 runs of `sample()` before and after with live readings: the median grows by at most 2 ms.

### Phase 3 — Build views name the showrunner and unit by short name  · status: todo

**Depends on:** Phase 1

#### Work Order

**Goal:** `/build:queue` and `/build:status` name the showrunner and the unit of each build step by the short names the user uses in sessions (Showrunner `builds`, `hana`; Unit `report`, `lfo`), from one shared label function.

**Spec:**

1. **One label function** (`scripts/message/roster.py`). The roster already holds the names: a showrunner's name is its registered session name (`UnderShowrunner.name`, `RunsProduction` → the entry's `name`), and a unit's name is its director's Claude session name (`DirectsUnit` → the entry's `name`, `UnderDirector.name`). These are the names herdr shows as workspace and tab. Move `SessionLabels` and `session_labels` from `scripts/build_queue/build_queue.py` (line ~36) into `roster.py` as `UnitLabels(session, showrunner, unit)` and `unit_labels(entries) -> dict[str, UnitLabels]`, keyed by Claude session id and Codex thread id, with the same rules; `—` where a relationship is missing. Add `unit_labels_for(session_ids, environment) -> dict[str, UnitLabels]` for a caller that knows Claude session ids and must stay fast: it reads only the session records, the showrunner registry and the production unit marks the roster already reads (`_newest_sessions`, `_registered_showrunners`, `_production_places`), never the process table, Codex meshes or accounts. For the same live Claude session both functions return the same labels; a test pins it. No other file composes a showrunner or unit name.
2. **`/build:queue`** (`build_queue.py`). The `Unit director` column is renamed `Unit` in both tables. Cells come from `roster.unit_labels`. Nothing else in the tables changes.
3. **`/build:status`** (`scripts/build_targets/build_targets.py`, `running` text line ~89). A running claim or hold whose record names a holder session gets ` (showrunner <name>, unit <name>)` after its `since <time>`, from `roster.unit_labels_for`: `mac: running nextest hana since 12:46 EDT (showrunner hana, unit inspectors)`. A holder the lookup cannot name, or a record with no holder session, keeps today's text. A failed lookup never fails the command: it keeps today's text.
4. **`/build:report`**: no change. It has no Showrunner or Unit cell; its Waiting cells name worktree folders, which are file paths.
5. **Docs.** `commands/build/queue.md`: `Unit` in place of `Unit director`. `commands/build/status.md`: the showrunner and unit after a running claim. `docs/as-built/build-memory-admission.md`: the Queue API bullet names `roster.unit_labels`.

**Files:**
- `scripts/message/roster.py` — `UnitLabels`, `unit_labels`, `unit_labels_for`
- `scripts/build_queue/build_queue.py` — uses `roster.unit_labels`; `Unit` header
- `scripts/build_targets/build_targets.py` — showrunner and unit after a running claim or hold
- `scripts/message/test_roster.py`, `scripts/build_queue/test_build_queue.py`, `scripts/build_targets/test_build_targets.py`
- `commands/build/queue.md`, `commands/build/status.md`, `docs/as-built/build-memory-admission.md`

**Workers:** `2 writers` — open workers, or new ones when Phase 2's are still busy.
- `impl` — `roster.py`, `build_queue.py`, `build_targets.py`, `commands/build/queue.md`, `commands/build/status.md`, `docs/as-built/build-memory-admission.md`
- `test` — `test_roster.py`, `test_build_queue.py`, `test_build_targets.py`. Fake the session records, the showrunner registry and the unit marks; never read the live roster or live sessions. Cases: a unit director, a Codex worker under it, a showrunner, an unmatched session, a style unit whose session name already leads with its showrunner's (`cargo-liner-style-types`); `unit_labels` and `unit_labels_for` agree on one session; the `Unit` header in both queue tables; a status claim with a named holder, with an unknown holder, with no holder session, and with a lookup that raises.

**Constraints from prior phases:** Phase 1 added the queue's identity columns and `session_labels`; this phase moves that function, so every expectation it changes on purpose needs its own `Assertion-Change: <path>::<test> — <why>` line (no `::*`). `roster.py` serves every production's messaging; its owner is named in the checkpoint notice's also-touches line. No file here overlaps Phase 2, so the two phases can run side by side.

**Acceptance gate:**
- `python3 -m unittest scripts.message.test_roster scripts.build_queue.test_build_queue scripts.build_targets.test_build_targets` green, and the production Merge tests green.
- `basedpyright` on the six Python files: `0 errors, 0 warnings`.
- Live check on natedev from the worktree: `/build:queue` shows `Unit` with short unit names; `/build:status` shows `(showrunner <name>, unit <name>)` after each running Mac claim whose holder runs.
- Speed: `/build:status` median of 10 runs grows by at most 0.2 s (now about 0.3 s); `/build:queue` does not get slower.
