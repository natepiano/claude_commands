# builds-report

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `/build:queue` names each step's showrunner, unit director and session, shows the bare step name in both tables, and gives free memory its own column.

> **As-built disposition: amend** — `docs/as-built/build-memory-admission.md` (Queue API and Queue order bullets)

> **Production: builds** — unit `report-unit`; production doc `docs/plans/builds-production.md`

## Source

2026-10-29 10:07 EDT

Own what the build log records and what its reports show: the build log, /build:report, /build:mac_report, and the /build:queue and /build:status tables. The user has build report changes coming; they come to this unit. Until they arrive, stand by.

2026-10-29 10:35 EDT (the user, after a `/build:queue` run, with three screenshots of its tables)

[Image #1] what is the 'clone88ci' session - i have no way to know or track it - we should have the build report show the showrunner, the unit director and the session - at least - so i can identify

[Image #2] the steps are output differently in the two tables - they need to be just the short version - nextest, clippy, as in the running

[Image #3] needs, free, should be their own columns

## Delegation Context

- **Project:** `~/.claude` — the build system scripts; this plan changes the `/build:queue` tables.
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

### Phase 1 — Queue tables name who runs each step  · status: todo

#### Work Order

**Goal:** `/build:queue` shows each step's showrunner, unit director and session; the Step column is the bare step name (`nextest`, `clippy`) in both tables; and Waiting has a separate Free column, so its Reason no longer repeats the needs and free amounts.

**Spec:**

1. **Identity columns.** Both tables get `Showrunner` and `Unit director` columns before `Session`:
   - Running: `# | Showrunner | Unit director | Session | Step | Folder | Promised | Using | Delta | State`
   - Waiting: `# | Showrunner | Unit director | Session | Step | Folder | Waiting since | Needs | Free | Reason`

   `build_queue.py` reads `roster.roster(os.environ, roster.ThisMachine())` once per run (the queue is natedev-local, so the other machine is not read). It runs that read in a thread at the same time as `memory_admit.queue_view()`, so the wall time is the slower of the two reads, not their sum. Import `roster` the way `scripts/hierarchies/measure.py` does (`from scripts.message import roster`). Do not change `roster.py`; read its public state classes.

   Match a step's `session_id` to one entry: a Claude entry when `entry.session` is `ClaudeSession` and its `session_id` is equal; a Codex entry when `entry.thread` is `CodexThread` and its `thread_id` is equal. The labels for a matched entry:
   - **Session** — `entry.name`.
   - **Showrunner** — `UnderShowrunner.name`; for `RunsProduction` (the session is a showrunner), `entry.name`; else `—`.
   - **Unit director** — `UnderDirector.name`; for `DirectsUnit` (the session is a unit director), `entry.name`; else `—`.

   A step with no match keeps today's Session text (the recorded session name, else the folder basename) and shows `—` in both new columns. When the roster read raises, or returns problems, the tables still print; one line after the Totals line says `Roster: <first problem or error>; Showrunner and Unit director show — for unmatched steps`. A clean read adds no line.

   Live readings at 10:18 EDT on 2026-10-29, to check against: `clone88ci` is Codex worker `organon-ci88`, unit director `organon`, showrunner `hana`; `merge-gate` is unit director `merge-gate`, showrunner `hana`; `lfo` is unit director `lfo`, showrunner `hana`; every one of the 21 steps read then matched a roster entry.

2. **Behind-head name.** The waiting Reason `behind <name> (waiting N min)` names the head by its Session label from item 1, not by its recorded session name or folder. `BehindHead` today carries `head_name` (`memory_admit.py` `read_queue`, line ~976). Add the head's `session_id` to `BehindHead` so that `build_queue.py` can resolve it, and keep `head_name` as the label for an unmatched head.

3. **Bare step name.** The waiter row stores `"<repo> <step>"` (`waiter_identity`, `memory_admit.py` line ~867: `"step": f"{repo} {step}".strip()`), while the reservation row stores the bare step. Fix it where the step is stored: `waiter_identity` writes the bare `step`. Then both tables show `nextest`, `clippy`, `mend`, `doc`. The `queue_bump` build-log row takes `moved.step`, so from now on it also records the bare step; its `folder` field still names the tree. Check that no other reader of a waiter's `step` field needs the repo prefix (`grep -n '"step"\|\.step\b' scripts/lint/memory_admit.py scripts/buildlog/*.py`); the waiter's `repository_name` field keeps the repo.

4. **Free column; shorter Reason.** Waiting gets a `Free` column with `view.available` in GiB (`12.1 GiB`), on each row. The table's Reason text drops the amounts that are now in Needs and Free:
   - `BehindHead`: `behind <name> (waiting N min)`
   - `NeedsMemory`: `<reserve> GiB kept for earlyoom`
   - `NeedsBuildBudget`: `builds use <charged> of the <budget> GiB budget`
   - `ReadyForAdmission`: `ready for the next admission check`
   - `ReadingUnavailable`: `memory reading unavailable`
   - `MemoryPressure`: unchanged (`memory_admit.pressure_text`)
   - `HeadAdmissionStatus`: the head text without its first `needs <need> GiB` and its `<available> free` amounts: for example `head: builds use 16.1 of the 21.0 GiB budget (starts when build use falls to 10.1 GiB); needs 13.9 GiB free; younger steps wait until 10:27`. The `needs <threshold> GiB free` part stays: it is the free memory the head must see (need plus reserve), a different number from Needs. Build this text in `build_queue.py` from the `HeadAdmissionStatus` fields; `head_wait_text` stays as it is (Invariants).

5. **Bump by the shown name.** `bump <selector>` also takes the Session label the table shows (for example `bump organon-ci88`). `build_queue.py` resolves a selector that equals exactly one shown Session label to that step's full `session_id`, then calls `memory_admit.bump_waiter` with it. Every other selector goes to `bump_waiter` unchanged. Two waiting rows that share the label are ambiguous: print the existing ambiguous error and move nothing. The result line names the step by its Session label.

6. **Command and as-built text.** `commands/build/queue.md`: name the new columns, say Showrunner and Unit director come from the session roster with `—` when no roster entry matches, say Step is the bare step, add the Free column, and add the shown Session name to the bump selectors. `docs/as-built/build-memory-admission.md`: in the Queue API bullet (line ~36) add the roster read and the bare waiter step; in the Queue order bullet (line ~37) add the shown Session label as a selector.

**Files:**
- `scripts/build_queue/build_queue.py` — roster read in parallel with the queue view, identity columns, Free column, table reason text, bump by Session label
- `scripts/lint/memory_admit.py` — `waiter_identity` writes the bare step; `BehindHead` carries the head's `session_id`
- `scripts/build_queue/test_build_queue.py` — tests for items 1–5 with a faked roster
- `scripts/lint/test_memory_admit.py` — waiter step and `BehindHead` field tests
- `commands/build/queue.md` — the command text
- `docs/as-built/build-memory-admission.md` — the Queue API and Queue order bullets

**Workers:** `2 writers` — no test lane (tests sit beside the code), so `test` opens as impl.
- `impl` — `scripts/build_queue/build_queue.py`, `scripts/lint/memory_admit.py`, `commands/build/queue.md`, `docs/as-built/build-memory-admission.md`
- `test` (opens as impl) — `scripts/build_queue/test_build_queue.py`, `scripts/lint/test_memory_admit.py`. Fake the roster with `mock.patch` on the roster function `build_queue.py` calls; never read the live roster. Cases: a Codex worker under a director and a showrunner; a unit director; a showrunner; an unmatched session (`—` and today's Session text); a roster that raises and one with problems (tables still print, one `Roster:` line); both tables show the bare step; the Free column and each reason text in item 4; `behind <label>` uses the head's Session label; bump by a unique Session label moves that step, and by a shared label moves nothing.

**Constraints from prior phases:** none (Phase 1).

**Acceptance gate:**
- `python3 -m unittest scripts.build_queue.test_build_queue scripts.lint.test_memory_admit` green, and the production Merge tests (`env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only` and the production basedpyright line) green.
- `basedpyright scripts/build_queue scripts/lint/memory_admit.py scripts/lint/test_memory_admit.py`: `0 errors, 0 warnings`.
- Live check on natedev from the worktree: `python3 scripts/build_queue/build_queue.py`. Each step whose session the roster lists (`~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py --here`) shows that entry's showrunner, unit director and session name; every Step cell in both tables is one bare word; Waiting has a Free column, and no Reason cell starts with `needs <amount> GiB; <amount> free`.
- Speed: run `build_queue.py` and `roster.py --here --json` back to back three times. The median `build_queue.py` wall time is at most the median roster wall time plus 0.5 s (before this phase: queue 2.2 s, roster 3.3 s on 2026-10-29 10:30 EDT).
