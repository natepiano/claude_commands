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

2026-10-29 14:12 EDT (the showrunner, approving verify-unit's Work Order for the copy folder name)

verify-unit's Work Order for the copy folder name (roster.py join `<showrunner>-<unit>`, drop a leading showrunner name from the unit name, drop a missing part, callers pass the director's session from owning_session(), 4 tests) is approved. Add it as the phase after your Phase 3, because it builds on Phase 3's unit_labels.

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
  - `scripts/message/roster.py` — `roster(environment, ThisMachine())` (line ~1504) returns `Roster(entries, problems)`; `RosterEntry` (line ~219) with `kind`, `role`, `name`, `session: ClaudeSession | NoClaudeSession`, `thread: CodexThread | NoCodexThread`, `showrunner: RunsProduction | UnderShowrunner | ProductionWithoutRunningShowrunner | NoShowrunner`, `director: DirectsUnit | UnderDirector | NoDirector` (classes at lines ~86–178)
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

### Phase 2 — Samples record CPU and disk pressure  · status: done

#### As-built

- `buildlog sample` reads `/proc/pressure/cpu`, `/proc/pressure/io` and `/proc/stat` once per 60 s sample and records seven fields: `cpu_stall_some_us`, `cpu_stall_full_us`, `io_stall_some_us`, `io_stall_full_us` (PSI totals, microseconds) and `cpu_idle_ticks`, `cpu_iowait_ticks`, `cpu_total_ticks` (USER_HZ). A missing file or a parse error makes only that reading `null`; the memory readings stay required, and the sample is still written.
- `sample.py` holds the paths `CPU_PRESSURE`, `IO_PRESSURE`, `STAT`; `CpuTicks(idle, iowait, total)` from `parse_cpu_ticks(text)`, which sums the first eight fields of the `cpu ` line (guest time is already inside user and nice) and raises `ValueError` on a missing or short line; `read_cpu_ticks(path) -> CpuTicks | Unmeasured`; and `read_pressure(path) -> PartialStallTotals`. The PSI result types are `StallTotals` and `PartialStallTotals`, shared by memory, CPU and IO.
- The `samples` table has the seven fields as `INTEGER` columns at `SCHEMA_VERSION` 23; older rows read NULL.
- `report.py` `sample_counter_deltas(connection, day, columns) -> list[SampleCounterInterval]` holds the one pairing rule for memory, CPU and IO counters: same host, gap at most `MAX_SAMPLE_GAP_S`, the host's latest earlier sample as the day's baseline, and a new `boot_id` counts the later counter from zero. `SampleCounterInterval.deltas` is keyed by counter name; a NULL at either end gives `CounterNotRecorded.VALUE`, which adds nothing to that counter or its denominator. `memory_pressure_section` uses it, with unchanged text and numbers.
- `cpu_disk_pressure_section(connection, day)` prints `### CPU and disk pressure` after Memory pressure, with `Source: 60 s machine samples (/proc/pressure/cpu, /proc/pressure/io, /proc/stat).` Each host with a counted pair gets `<host>: CPU some X%, full Y%; IO some X%, full Y%; CPU idle X%, iowait Y% (of <duration> sampled)`. A PSI share divides by the wall time of that counter's counted pairs; idle and iowait divide by the `cpu_total_ticks` delta of the same pairs. A share with no denominator is `—`; with no counted host the section says `CPU and IO samples: none.`
- One sample costs about 0.11 ms more (20-run median 0.234 ms → 0.347 ms).

**Files:**
- `scripts/buildlog/sample.py` — the three paths, `CpuTicks`, `parse_cpu_ticks`, `read_cpu_ticks`, `read_pressure`, `StallTotals`, `PartialStallTotals`, the seven record fields
- `scripts/buildlog/index.py` — seven sample columns, schema 23
- `scripts/buildlog/report.py` — `sample_counter_deltas`, `SampleCounterInterval`, `CounterNotRecorded`, `cpu_disk_pressure_section`
- `scripts/buildlog/test_sample.py`, `scripts/buildlog/test_index.py`, `scripts/buildlog/test_report.py` — faked readings, schema rebuild, section cases
- `scripts/buildlog/test_memory_records.py` — its subprocess fixture points the three new paths at scratch files
- `commands/build/report.md`, `docs/as-built/buildlog-memory-stalls.md` — the new section and fields

**Gotchas:**
- A test that runs `sample()` in a subprocess must point `CPU_PRESSURE`, `IO_PRESSURE` and `STAT` at scratch paths, as `test_memory_records.py` does, or it reads live `/proc`.
- The `(of …)` duration is the union of pairs where any PSI counter counted, so a host with only tick readings shows `0.0 s` sampled beside valid idle and iowait shares.
- A `SCHEMA_VERSION` raise changes `test_index.py`'s schema expectation, which needs its own `Assertion-Change:` line.

**Ruled out:** wall time times CPU count as the tick denominator — it goes wrong when CPUs go offline; picking counters by tuple position in `report.py` — they are keyed by name.

### Phase 3 — Build views name the showrunner and unit by short name  · status: done

#### As-built

- `roster.py` holds the one label rule. `UnitLabels(session, showrunner="—", unit="—")` is frozen and its fields are whitespace-collapsed. Showrunner is `UnderShowrunner.name`, or the entry's name for `RunsProduction`; unit is `UnderDirector.name`, or the entry's name for `DirectsUnit`; else `—`. These are the short names herdr shows (`builds`, `report`).
- `unit_labels(entries) -> dict[str, UnitLabels]` keys the full roster by Claude session id and Codex thread id. `unit_labels_for(session_ids, environment) -> dict[str, UnitLabels]` is the fast path: it reads session records, the showrunner registry through `showrunners.registered_showrunners(known_sessions=...)` and unit marks through `unit_lookup.session_unit_marks` (each record's PID environ only), never the process table, Codex meshes or accounts. A session whose `CLAUDE_CODE_SESSION_ID` names another known session resolves under that director. Unknown ids are left out; any error returns `{}`. `unit_lookup.group_marked_units` holds the slug-rename and duplicate-mark rule for both paths, and a test pins that both paths agree.
- `/build:queue` names the column `Unit` in both tables, from `roster.unit_labels`; the roster problem line ends `Showrunner and Unit show — for unmatched steps`.
- `/build:status`: `targets.py` carries `HolderSession(session_id) | NoHolderSession` as `holder_session` on `RunningJob` (the claim record's `holder_session`) and `Held` (the local hold key, or a Mac block's `RecordedSession` name). `build_targets.main` collects the holders, calls `unit_labels_for` once, and `format_state` / `running_job_text` add ` (showrunner <name>, unit <name>)` after `since <time>`. No holder session, an unknown holder or a lookup that raises keeps the old line. A status run costs about 0.10 s more (median 0.204 → 0.307 s).
- `roster.py` imports `scripts.shutdown.remote` by package and does not put `scripts/shutdown` on `sys.path`; `account`, `agent_accounts` and `note_file` load lazily. `scripts/shutdown/inventory.py` appends its own folder after existing `sys.path` entries for its sibling imports.

**Files:**
- `scripts/message/roster.py` — `UnitLabels`, `unit_labels`, `unit_labels_for`, package import of `scripts.shutdown.remote`
- `scripts/production/unit_lookup.py` — `group_marked_units`, `session_unit_marks`
- `scripts/production/showrunners.py` — `registered_showrunners(known_sessions=...)`, `DiscoverSessions`
- `scripts/build_queue/build_queue.py` — `roster.unit_labels`, `Unit` header
- `scripts/build_targets/targets.py` — `HolderSession`, `NoHolderSession` on `RunningJob` and `Held`
- `scripts/build_targets/build_targets.py` — `unit_label_suffix`, one `unit_labels_for` call per run
- `scripts/shutdown/inventory.py` — appends its own folder for sibling imports
- `scripts/message/test_roster.py`, `scripts/build_queue/test_build_queue.py`, `scripts/build_targets/test_build_targets.py`, `scripts/build_targets/test_targets.py`, `scripts/production/test_unit_lookup.py`, `scripts/production/test_showrunners.py`, `scripts/shutdown/test_inventory.py` — faked records, registry and marks; fast-path parity; holder cases; fresh-process import regressions
- `commands/build/queue.md`, `commands/build/status.md`, `docs/as-built/build-memory-admission.md` — the `Unit` column, the status suffix, `roster.unit_labels`

**Binds later work:** `UnitLabels`, `unit_labels` and `unit_labels_for` live in `roster.py`; `—` marks a missing relationship. Copy folder names build on `UnitLabels`.

**Gotchas:**
- `roster.py`'s module-level `sys.path` edits reach every importer: a change there must also run the herdr and shutdown folder suites.
- buildlog and shutdown each have a `record.py`; with `scripts/shutdown` ahead on `sys.path`, `import record` loads the wrong one.
- A status line joins a claim to its holder by the recorded holder session; `what` and `since` can match a stale claim.
- Status timings under machine load vary from 0.3 to 2 s; measure on a quiet machine.

**Ruled out:** a label fast path with its own copy of the slug and mark rules — it drifts from `unit_lookup` and `showrunners`; a package-import migration of the other shutdown modules — the record name clash belongs to shutdown-unit.

### Phase 4 — Copy folders take one showrunner-unit name  · status: done

**Depends on:** Phase 3

#### Work Order

**Goal:** One function in `roster.py` gives the `<showrunner>-<unit>` folder name that verify-unit's copies helper puts in `/tmp/claude/copies/<showrunner>-<unit>/<phase>-<purpose>`, from Phase 3's `UnitLabels`, so no caller joins names itself.

**Spec:**

1. **`UnitLabels.folder_name() -> str`** (`scripts/message/roster.py`). It joins the showrunner and unit names with `-`: showrunner `hana`, unit `lfo` gives `hana-lfo`.
2. **No doubled showrunner.** When the unit name is the showrunner name, or starts with the showrunner name and `-`, the unit name alone is the folder name: showrunner `cargo-liner-style`, unit `cargo-liner-style-types` gives `cargo-liner-style-types`.
3. **A missing part is dropped.** A `—` part is left out with its hyphen: showrunner `builds` and no unit gives `builds`; no showrunner and unit `lfo` gives `lfo`. With both missing, the session name is the folder name.
4. **Callers pass the director's session.** The docstring says: look up labels with `unit_labels_for([<session id from build_hold.owning_session()>], environment)`, so a Codex worker resolves to its unit director. This phase changes no caller; verify-unit's copies helper is the first.

**Files:**
- `scripts/message/roster.py` — `UnitLabels.folder_name`
- `scripts/message/test_roster.py` — four tests

**Workers:** `1 writer` — `impl2` (it wrote Phase 3's `UnitLabels`): both files. Tests build `UnitLabels` values directly; they read no live roster or session. Cases: plain join; unit that leads with its showrunner's name; one part missing (each side); both missing.

**Constraints from prior phases:** Phase 3 put `UnitLabels`, `unit_labels` and `unit_labels_for` in `roster.py`; `—` marks a missing relationship. `roster.py` belongs to natedev's `scripts/message/` owner (shutdown-unit), so the checkpoint notice carries its also-touches line, trial-merged against origin/natedev and origin/natedev-shutdown. A changed existing expectation needs its own `Assertion-Change: <path>::<test> — <why>` line.

**Acceptance gate:**
- `python3 -m unittest scripts.message.test_roster` green, and the production Merge tests green.
- `basedpyright scripts/message/roster.py scripts/message/test_roster.py`: `0 errors, 0 warnings, 0 notes`.
- Live check on natedev from the worktree: `unit_labels_for` for this session's id, then `folder_name()`, prints `builds-report`.
