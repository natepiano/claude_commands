# hierarchies

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready; long-running.** Find the agent hierarchy that delivers Hana fastest under the user's constraints: measure today's hierarchy, collect the constraints, propose the best hierarchy with a step-by-step change to it, then build and measure one approved step at a time.

> **Production: sdlc** — unit `hierarchies-unit`; production doc `docs/plans/sdlc-production.md`

## Source

2026-10-20 16:36 EDT

Do we have a unit director that is dedicated to, I guess it's showrunners and producers, but they're all working on different things, and some are working on process, some on configuration management, some on code. So I need to have someone consulting on the hierarchies that we work on. Maybe I'll give that to you right now to assign to a new unit director for hierarchies. Basically how to simplify and improve communication so that we are working on the most important thing and achieving the overall goal of improving our HANA product. So let's get a new unit director to start the design work on that.

2026-10-20, via the showrunner: "It doesn't have to be a simpler hierarchy it needs to be the best hierarchy. The one that helps us deliver the product fast according to my constraints"

## What exists today (measured 2026-10-20 16:30–16:45 EDT by the plan author)

- **Hierarchy:** the user (executive producer) → 4 showrunners (`showrunners.py tree`: build-followups on natedev, remove-running-section on cargo-liner with cargo-liner-style below it, tool-based-ui on hana) → 37 unit directors → about 171 Codex workers. Producers, nightly review, efficiency, postmaster, notifiers, change review, proposals and the plan backlog sit beside the tree.
- **Work on Hana:** 10 of 38 live units (26%) work on the Hana product, all in tool-based-ui. build-followups has 19 live units and none on Hana; 11 of them finished their runs and keep their sessions open. About 113 of 171 workers sit in Hana worktrees, and 59 of those belong to runs whose director is not running.
- **Waiting on the user:** 314 unreviewed changes, about 145 open proposal issues, 123 proposed efficiency items, 32 open question files (`~/.local/state/showrunner/counts`, `~/rust/hanadocs/change-review/`, `~/.local/state/nightly-review/ledger.md`, `~/.local/state/notify/questions/`).
- **Traffic:** 11,187 agent messages 10-01 to 10-20 (325–996 a day since 10-06), mostly automation (`~/.local/state/message/log.jsonl`). 133 phone pushes since 10-05; on 10-20, 12 of 23 went out after a failed review (`~/.local/state/notify/pushover.jsonl`, `review.jsonl`).
- **Priority:** `~/rust/hanadocs/prioritization goals.md` orders four goals, 1 Ship Hana first; `/issue` and `/prioritize` rank 538 open issues into `backlog-rankings.jsonl`. No production doc cites a rank or a goal. 11 active issues link to hana units; rank 4, 8 and 10 have no unit; frame-time, startup and cables have no issue.
- **Records of friction:** memories `next-thing-never-auto-advances`, `proposals-accumulate-unshown`, `relay-directly-not-via-user` (5 recurrences 09-09 to 10-16), `open-questions-as-a-count`, `process-redesigns-are-questions`, and the attempts logs for push text review (9, 9, 6 and 4 rounds).

## Decisions

- **The target is the best hierarchy, not the simplest** (user, 2026-10-20): the one that delivers Hana fastest under the user's constraints. Fewer layers is a means, never the measure.
- **Every change to roles, promotions, renames or responsibilities is a question for the user** (user, 2026-10-20, memory `process-redesigns-are-questions`). This unit proposes; the showrunner brings each proposal to the user; nothing in the live process changes before the user approves it. Measuring and writing the design doc need no approval (plan author).
- **One design doc** holds the constraints, the map, the baseline, the candidates and the steps: `docs/plans/build-followups-hierarchies-design.md` (plan author). It becomes the as-built at run end.
- **A flat organization** (user, 2026-10-20, via the showrunner: "I want a flat organization"): top-level showrunners are peers. Today and soon: build-followups (natedev), remove-running-section (cargo-liner), tool-based-ui (hana), sdlc, and guilds, whose four guild heads (`designers`, `reviewers`, `showrunners`, `units`) are its unit directors (user G1b, `docs/plans/sdlc-production.md`; `docs/as-built/guilds.md`). Every candidate counts all of them.
- **Scope beside sdlc** (user, 2026-10-20, sdlc production rules): this unit designs the structure — which productions and showrunners exist, who decides priority, how work is admitted, how capacity is shared and how the user is reached — and measures it. sdlc's roles-unit owns each role's responsibilities and the names of things; guilds-unit the designer and reviewer roles and guilds; phases-unit the phase process; fix-rounds-unit repair rounds. This unit reads their plans and does not do their work; roles-unit and guilds-unit read this plan before they write theirs. A step that changes a responsibility or a name goes to roles-unit through the showrunners.
- **One measure script, run again after each step** (plan author): `scripts/hierarchies/measure.py`. A step counts as done only when its measure moved.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions. This plan measures the agent hierarchy and changes it one approved step at a time.
- **Project started:** 2026-10-20T20:46:48.678+00:00
- **Stack:** Python 3.13, standard library only; Markdown.
- **Layout:**
  - `scripts/hierarchies/` — `measure.py`, `push_audit.py`, `worker_owners.py`, `log_records.py` (shared log helpers; `measure.py` imports `push_audit.py`, never the reverse) and their tests
  - `config/hierarchies/sessions.toml` — keys and display names of top-level sessions
  - `docs/plans/build-followups-hierarchies-design.md` — the design doc (new)
  - read only, outside the repository: `~/.local/state/message/log.jsonl`, `~/.local/state/notify/` (`pushover.jsonl`, `review.jsonl`, `questions/`), `~/.local/state/showrunner/` (`counts`, `outstanding`, `<production>/`), `~/.local/state/unit-board/*.json`, `~/.local/state/nightly-review/ledger.md`, `~/.local/state/plan-backlog/ledger.md`, `~/rust/hanadocs/` (issues, `backlog-rankings.jsonl`, `prioritization goals.md`, `change-review/`)
- **Key files:**
  - `scripts/production/showrunners.py` (`list`, `tree`) — every production and its doc
  - `scripts/production/units_table.py --production <doc>` — live unit rows and state
  - `scripts/direct/progress_history.py` — a unit's phase records and durations
  - `scripts/buildlog/index.py` — build and test runs by repository
  - `scripts/usage/usage_report.py`, `collect.py` — tokens by session
  - `scripts/agents/codex_mesh.py` — the worker roster
  - `scripts/lib/user_zone.py` — user-facing times
  - `docs/production_format.md`, `commands/showrunner/produce.md`, `commands/unit/direct.md` — the role contracts
  - `~/.claude/docs/decision_criteria.md` — what reaches the user
- **Test lanes:** `scripts/hierarchies/` — `test_*.py` beside the script.
- **Test:** `python3 -m unittest discover -s scripts/hierarchies -p 'test_*.py'`, from the worktree root. Gate: `~/.claude/scripts/lib/py scripts/production/merge_gate.py --tests-only` from the worktree root (memory `shared-module-full-gate`).
- **Lint:** `basedpyright scripts/hierarchies` passes when its output ends `0 errors, 0 warnings, 0 notes`; it exits 3 in every checkout, so the exit status says nothing.
- **Invariants:**
  - Measuring reads every source read-only. It sends no message, push or alert, and starts no session (plan author: a measure must not add traffic).
  - Tests use fixtures; they never read the real `~/.local/state`, `~/rust/hanadocs` or transcripts.
  - Each number states its window and its count; medians and p90 come with n.
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`).
  - User-facing times use `scripts/lib/user_zone.py` (America/New_York, user 2026-10-12); log stamps stay UTC.
  - All text uses Simplified Technical English (user, 2026-10-18).

## Phases

### Phase 1 — One command measures how fast the hierarchy delivers Hana, and the design doc lists the user's constraints  · status: done
**Depends on:** none

#### As-built

- `scripts/hierarchies/measure.py baseline [--since DATE] [--until DATE]` prints one aligned table per group: capacity, resource use, phase flow, issue flow, attention, pending attention and rank coverage. Each row shows its window and n. The default window is the 7 days to now, end excluded. A run on natedev takes about 23 s.
- Each measure is one function over typed domain records. Loaders read every source read-only. A missing or corrupt source prints a source-problem row and the command continues. The command keeps no state.
- Items waiting on the user come from showrunner/outstanding through `dailies_render.read_outstanding`. Held-push files are not questions. Messages are sorted by their kind field. A deleted Hana worktree is classified from its run record, with a name fallback.
- The design doc `docs/plans/build-followups-hierarchies-design.md` holds Constraints C01-C50, each with its source and a `user` or `derived` mark; Map (roles, productions, channels, ownership beside sdlc); Baseline (run 2026-10-20 17:24 EDT); and Questions for the user Q1-Q5.

**Files:**
- `scripts/hierarchies/measure.py` — the measure command, typed records, read-only loaders, measure functions
- `scripts/hierarchies/test_measure.py` — 19 fixture tests, source-problem rows included
- `pyrightconfig.json` — the `scripts/hierarchies` execution environment
- `docs/plans/build-followups-hierarchies-design.md` — the design doc

**Binds later work:** The comparison command reuses the baseline CLI shape, the measure functions over typed records and the same source-problem rows. Flow-wait rows stay `not recorded` until a source records wait kinds. The candidate designs read the Constraints, Map and Baseline sections.

**Gotchas:**
- basedpyright exits 3 in every checkout; read its last line, not its exit code.
- No source records wait kinds, issue activation times or question answer times. Those rows print `not recorded` and name the source that would hold them.
- 51 of 187 workers record no cwd. They count as unclassified, not as Hana or other.
- The merge gate test `accounts/test_cutover_home` checks access times. It fails under heavy load and passes when run alone.

### Phase 2 — The design doc recommends the best hierarchy and the steps to reach it  · status: done

#### As-built

- The design doc holds Candidates, Comparison, Recommendation and Steps. The user's review decisions HI1-HI11 are folded in.
- Candidate A (today's tree, repaired) runs as an experiment. Candidate B does not start. Candidate C is dropped.
- The user approves Steps 1, 2 and 4. Each step has a Hana link, a measure and a review point. Constraints C53-C57 record the new rules.
- sdlc is a top-level showrunner. guilds is its child, and the four guild heads are units of guilds.
- `measure.py baseline` splits closures by project and leaves out declines. It also reports Hana merges per week, rank coverage of open issues, date-time window flags and one missing-value type per domain.

**Files:**
- `docs/plans/build-followups-hierarchies-design.md` — Constraints, Map, Baseline, Questions, Candidates, Comparison, Recommendation, Steps.
- `scripts/hierarchies/measure.py` — Baseline measures. Missing-value types: `TimeNotRecorded`, `DurationNotRecorded`, `ScoreNotRecorded`, `MergeHistoryNotRecorded`. Re-run with `~/.claude/scripts/lib/py scripts/hierarchies/measure.py baseline` from the worktree root (about 23 s).
- `scripts/hierarchies/test_measure.py` — tests for those measures.

**Binds later work:** The unit director writes each step's result into the design doc. The Step 2 work waits on phases-unit's HI7 phase, and must check that the wait record has a holder field. The four-week review counts a Hana wait as non-Hana-caused only when a record names the holder. The four-week review waits on the two-week re-runs of Steps 1 and 4. The Step 4 keep rule goes to models-unit as a requirement.

**Gotchas:** The closure count treats any reason whose first word is "declined" as a decline. The run recorder writes `phase_instance_id` and `phase_id`, never `phase`. Each missing value has its own type, and all of them print "not recorded".

**Ruled out:** candidate B, a shared rank order with a Hana-cost test (HI10); candidate C, capacity-led with natedev allocating workers (HI11); a fixed Hana share or measured Hana cost (HI2); a daily review budget (HI3); a size threshold for the four-week review's proposal (HI10).

### Phase 3 — Each user push after a failed text check names its approved exception, and unapproved paths are fixed  · status: done

#### As-built

- `push_audit.py report` reads `pushover.jsonl` and `review.jsonl`, read-only, and lists each delivered push that followed a failed text check, with its class (ApprovedPriorityTwo, ApprovedRegisteredText, Unapproved), sending path and owner.
- Baseline window: 64 delivered, 17 after a failed check (priority-2 1, registered 16), Unapproved 0. 2026-10-21 20:23 to 2026-10-28 20:23 EDT: 55 delivered, 12 (priority-2 3, registered 9), Unapproved 0. No sending path needed a fix.
- `review.py`: a rewrite check that returns unreviewed keeps the failures of the draft that was sent (it dropped them before).
- User ruling 2026-10-28: `nate.adhoc` loses the registered-text exception; messaging-unit builds it.

**Files:**
- `scripts/hierarchies/push_audit.py` — `report`; owners from current production tables (`ProductionOwnership`: exact names, and `-unit` aliases for `send.py in <session>` names only).
- `scripts/hierarchies/test_push_audit.py` — tests on real log record shapes.
- `scripts/notify/review.py`, `test_review.py` — messaging-unit's files, edited with showrunner approval.
- `docs/plans/build-followups-hierarchies-design.md` — Step 1 result and corrected counts.

**Binds later work:** Phase 6 re-runs `report` two weeks after the merge. After messaging-unit's change, a failed `nate.adhoc` check is held, not delivered. The push-audit duty went to sdlc for roles-unit.

**Gotchas:** Neither log records a key. Older sends can show a session no current production names. `measure.py` counts 16 for the Baseline by a narrower rule; the audit is the reference. The audit reads this machine's logs only.

**Ruled out:** changing an approved exception without the user (nate.adhoc went to the user); limiting the `validate()` change to push review (it adds failure quotes to held-question text and breaks nothing).

### Phase 4 — The measure reads wait records and merge receipts, and the 4-week experiment starts  · status: todo
**Depends on:** Phase 2; and sdlc gate G1, the wait records from phases-unit:
- **Supplier:** phases-unit's HI7 phase, its Phase 6 (sdlc gate G1, 2026-10-28); its phase 7 commit 820357a2 sets the reader's schema and clears G1 when merged (sdlc, 2026-10-29).
- **Record contract:** the shape sent to phases-unit on 2026-10-28, given under Spec below. phases-unit picks the file and the writer and sends both, with the commit, when its phase lands.
- **Clears when:** that phase's checkpoint is merged to `origin/sdlc` and on `~/.claude` main, its message names the record file and commit, and the file holds at least one wait record and one merge receipt written by a real phase. Until then this phase does not start.

#### Work Order

**Goal:** design Step 2 (HI7, approved as a 4-week experiment): `measure.py` reads the wait records and merge receipts that phases-unit builds, and the doc names the experiment's start and review date.

**Hana link:** direct. The records show which waits slow Hana phases.

**Spec:**
- The record shape went to phases-unit on 2026-10-28 (showrunner ruling 3) the design needs: per wait, phase, owner, UTC start and end, and one kind (`WaitingForQuotaRefusal`, `WaitingForMemoryOrSlot`, `WaitingForMergeGate`, `WaitingForUser`, `WaitingForSupplier`, `ReadyWithoutCapacity`); per merge, a receipt (project, issue or plan phase, commit, target branch, merge time). The duty text goes to roles-unit. phases-unit picks the file and writer; this phase adapts to it.
- `measure.py baseline` prints one `Hana wait / <kind>` row per supplied kind (six) and a receipts row: minutes by kind with n. Missing wait history (`HistoryNotRecorded`, never zero), a merge with no receipt (`ReceiptMissing`) and a wait whose cause is not recorded are distinct domain states at the loader boundary. The summary keeps its five lines; the rows print under `--full`. Readers are read-only typed loaders; a corrupt source prints a source-problem row.
- Design doc: the experiment's start (first records on `~/.claude` main) and its review date, four weeks later.
- Phase 6 counts a Hana wait as non-Hana-caused only when a record names the non-Hana holder of the account, slot or memory. Check that phases-unit's record shape carries that holder; if it does not, ask phases-unit (through the sdlc showrunner) to add it before the experiment starts.
- Source: `progress_history.py waits --session-dir <S> [--since] [--until]`, schema at phases-unit 820357a2. External waits come from `wait_attribution.jsonl` as well as the run files; a production handoff wait has no phase.
- Uncertain coverage (`uncertain_phase_n`, `attribution_miss_n`, each with its reason and candidates) prints as its own rows, never as a covered zero. Candidate phases carry no `wait_n`.
- An unclosed wait has no end time: print it as unknown-ended with n, and leave it out of every completed-duration total.

**Files:**
- `scripts/hierarchies/measure.py`, `scripts/hierarchies/test_measure.py`
- `docs/plans/build-followups-hierarchies-design.md` — Step 2 start and review date

**Workers:** `1 writer + 1 tester`
- `impl` — `measure.py` and the doc
- `test` — `test_measure.py`: fixtures for each wait kind, a window with no records, a merge with and without a receipt, each of the six kinds and each missing state, uncertain coverage that is not counted as zero, an unclosed wait left out of the minutes, and an external handoff wait with no phase

**Constraints from prior phases:** the Baseline rows print `not recorded` today; `--since` and `--until` take dates and date-times. From phases-unit's architect review (sdlc, 2026-10-29): uncertainty never becomes zero waits, and the reader never invents a duration for an unclosed wait. From Phase 7: shared log-record helpers (`Window`, `MeasureRow`, `SourceProblem`, `field_*`, `record_time`, `object_record`, `table`, `window_bounds`, `TimeNotRecorded`) live in `scripts/hierarchies/log_records.py`; new readers import them from there, and `push_audit.py` never imports `measure.py`.

**Acceptance gate:** unit tests green; per-slot `basedpyright` ends `0 errors, 0 warnings, 0 notes`; merge gate green; `measure.py baseline --full` over the first records prints real wait and receipt rows. **Measure:** share of Hana phase time with a recorded wait kind, and share of merges with a receipt. **Review point (C53):** four weeks after the first records land; Phase 6 keeps or drops the step on it.

### Phase 5 — A run end ends only workers whose unit director is gone or whose owner cannot be found  · status: done

#### As-built

- `worker_owners.py report` names a class for each Codex worker: OwnedLive, PooledReviewer, DirectorGone or OwnerUnknown. On 2026-10-28 21:32 EDT: 185, 141, 0, 0 (n=326; the Baseline had 51 with no known owner).
- `recover` finds owners from launcher files and director messages and saves them in `recovered.json`; a saved owner stays when its first evidence goes away.
- `end` previews by default. An applied end acts only on the selected class (DirectorGone or OwnerUnknown), keeps attached, ended and unrecorded workers, ends nothing on its first run, and logs each decision in `ends.jsonl`.
- `measure.py baseline` prints a five-line summary; `--full` adds the tables (showrunner C21 return).

**Files:**
- `scripts/hierarchies/worker_owners.py` — `report`, `recover`, `end`; classes `WorkerClass`, `WorkerTurnState` (with `Ended`), `WorkerKept`.
- `scripts/hierarchies/test_worker_owners.py` — tests; no real end command runs.
- `scripts/hierarchies/measure.py`, `test_measure.py` — `baseline_summary`, `--full`.
- `docs/plans/build-followups-hierarchies-design.md` — Step 4 result.

**Binds later work:** Phase 6 re-runs `report` two weeks after the merge. Each end sends `--expected-thread-id` and `--idle-only`; models-unit's `codex_mesh.py end` must accept both and check them under its lock. Until then every end is refused and nothing is ended.

**Gotchas:** The live state folder holds `recovered.json` and `ends.jsonl`; tests use temporary folders. basedpyright exits 3 because the configured `.venv` is absent; only the footer counts.

**Ruled out:** ending a named worker of a live director; ending OwnerUnknown before recovery fails; editing `codex_mesh.py` or `end_session.sh` from this unit.

### Phase 6 — The four-week review: which waits slow Hana, which links helped, and which trial steps stay  · status: todo
**Depends on:** the checkpoints of Phases 3, 4 and 5, and the three review records: the Phase 3 and Phase 5 re-runs, which the unit director runs on 2026-11-12 (two weeks after both merged into sdlc as 3c5dea67 on 2026-10-29) and records in the design doc under Steps 1 and 4 (the push-audit re-run also confirms that `measure.py` and `push_audit.py` agree), and four weeks of Phase 4 records. The phase starts on the latest of these dates, never before.

#### Work Order

**Goal:** design Step 5: the doc reports which waits slow Hana (HI7), which Hana links helped (HI2), and keep or drop for each trial step (HI1).

**Hana link:** direct. The review keeps a step only by its effect on Hana.

**Spec:**
- `measure.py compare --before <Baseline window> --after <the four-week window>`: the same rows for both windows, each with before, after and the change; `not recorded` rows stay `not recorded`. The windows differ in length (7 days and 28 days), so each count row (merges, closures, pushes, waits) prints a rate per week, never a raw total, and the change is between the two rates. A row whose source covers only part of a window prints its covered days beside the rate and is marked partial.
- Set quota and memory waits beside same-hour non-Hana tokens on the same role account and non-Hana build memory on natedev. If the records show non-Hana work slowing Hana, bring a new proposal (HI10); there is no size threshold. Records show it only when a record names a non-Hana cause for a Hana wait: a `WaitingForSupplier` wait whose record names non-Hana work, or a quota or memory wait whose record, or the slot or hold record that covers the whole wait, names a non-Hana holder of that account, slot or memory. Load in the same hour is context, never proof. A wait with no named cause is reported as cause not recorded, with its minutes and n, and does not count. Report each wait with a named non-Hana cause with its minutes and n. If the Phase 4 record shape has no field for the holder, Phase 4 asks phases-unit to add one.
- Design doc section **Results**: per step, the measure, its change, keep or drop. The link records (Step 3) set beside Hana merges and closures.

**Files:**
- `scripts/hierarchies/measure.py`, `scripts/hierarchies/test_measure.py` — `compare`
- `docs/plans/build-followups-hierarchies-design.md` — Results

**Spec additions (architect review after Phase 7, 2026-10-29):**
- Exception at delivery time: `push_audit.classify()` returns `ApprovedRegisteredText` for any recorded text id, also a normal-priority `nate.adhoc` delivery after its exception ended. Classify each delivery by the exception in force at its delivery time (the policy's effective boundary is the messaging-unit c73a2d02 merge); keep recorded exception evidence apart from approval. Fixtures: a send before the boundary, one after it, and a held one.
- Coverage: a rate per week needs the covered days of each source. Give recorded coverage, an unavailable source and unknown coverage distinct domain types (no bare `None`); event timestamps alone never prove continuous coverage. Fixtures: empty but covered, partly covered, missing, corrupt.
- Compare: load the notification sources once and call `push_audit.audit()` per window, so reviews and silence records outside a window still pair. The failed-check count is `len(entries)`; the attention row's n is delivered pushes. Fixtures: a review across a window boundary, a delayed delivery, a rewrite, a held `nate.adhoc` review with no delivery.
- A missing or malformed `config/hierarchies/sessions.toml` stays visible in `compare` as a source problem naming the file; valid delivery counts never become zero and an attribution failure is never missing delivery history.

**Workers:** `1 writer + 1 tester`
- `impl` — `measure.py`, `push_audit.py` and the doc
- `test` — `test_measure.py` and `test_push_audit.py`: `compare` on fixtures with known before and after values, and the fixtures above

**Constraints from prior phases:** shared log helpers live in `scripts/hierarchies/log_records.py`; `measure.py` imports `push_audit.py`, never the reverse; session names come only from `config/hierarchies/sessions.toml`. The wait records come from `progress_history.py waits` (phases-unit 820357a2) through the Phase 4 loader and types.

**Acceptance gate:** unit tests green; per-slot `basedpyright` ends `0 errors, 0 warnings, 0 notes`; merge gate green; Results holds real numbers for each step, judged on the measure and review point each of Phases 3–5 names. **Measure and review point:** this phase is the review point for Steps 1, 2 and 4. At phase end the unit director sends the showrunner the Results and the keep-or-drop list as one question for the user.

### Phase 7 — The baseline measure and the push audit count failed checks by one rule, and session names come from config  · status: done

#### As-built

- `measure.py` takes its delivered-push and after-failed-check counts from `push_audit.audit()` (`AuditReport.delivered_pushes`, `AuditReport.entries`), so both reports use one rule: 17 of 64 for the Baseline window and 12 of 55 for 2026-10-21T20:23 to 2026-10-28T20:23.
- Shared log-record helpers live in `log_records.py`; `measure.py` imports `push_audit.py`, and `push_audit.py` never imports `measure.py`.
- `push_audit.py` reads bare top-level session names from `config/hierarchies/sessions.toml` into `TopLevelSessions` (key and display name); attribution uses the key, and printed text uses the display name. A missing or malformed file is a source problem.
- A held `nate.adhoc` review with no delivered push counts nowhere.

**Files:**
- `scripts/hierarchies/log_records.py` — `Window`, `MeasureRow`, `SourceProblem`, `TimeNotRecorded`, field readers, `record_time`, `object_record`, `table`, `window_bounds`
- `scripts/hierarchies/measure.py`, `test_measure.py` — attention rows from the audit; agreement tests by day
- `scripts/hierarchies/push_audit.py`, `test_push_audit.py` — `TopLevelSession`, `TopLevelSessions`, `read_sessions`, `owner_label`; a test fails when a configured session name is a string literal in `push_audit.py`
- `config/hierarchies/sessions.toml` — session keys and display names (today `hana`)

**Binds later work:** a later push count or comparison calls `push_audit.audit()`; it never adds a second reader or rule. New session names go only in `sessions.toml`.

### Phase 8 — The worker ownership report and the push audit open with what a person needs  · status: todo
**Depends on:** Phases 5 and 7 (showrunner returns of C443, C446 and C447, 2026-10-29: runs next, as a small checkpoint after Phase 7).

#### Work Order

**Goal:** both reports show a person a short answer first. `worker_owners.py report` always finishes and ends with its Totals line. `push_audit.py report` opens with one summary line and puts its tables under `--full`, as `measure.py baseline` does. The session-name check changes the name and sees the change.

**Hana link:** indirect. The Phase 6 review re-runs both reports; a report that stops on one bad file, or buries its total in 25 long rows, gives it no usable number.

**Spec:**
- `worker_owners.py` (C443):
  - An empty or unreadable source file (seen live: an empty `~/.local/state/review-pool/build-followups/mesh/0/mesh_roster.json`, "Expecting value: line 1 column 1 (char 0)") is a source problem. The report still prints every worker row it could read.
  - Text output order: worker rows, then source-problem lines, then the timer line, then the Totals line last. Totals also names the count of source problems (`source_problems=N`).
  - `report` and `recover` exit 0 when they print the report, source problems included. `end` keeps its rules: no worker is ended while any source problem exists, and a failed end exits 1.
  - Timers: by default the report prints one line, `Timers: <n> registered, <m> with no recorded request (the showrunner checks these; --full lists each)`. `--full` prints each timer line as today. `--json` is unchanged.
- `push_audit.py report` (C446): by default prints a short summary like `measure.py`'s `baseline_summary`: the window; delivered pushes and pushes after a failed text check, with the count per class (ApprovedPriorityTwo, ApprovedRegisteredText, Unapproved); the source-problem count and `Use --full to print the tables.` `--full` prints the summary, then today's tables. The numbers are the same in both forms.
- Session-name check (C447): the Change-Review row changes `display_name` in `config/hierarchies/sessions.toml` to another word, runs the full `push_audit.py report --full` command over a window with a hana row (2026-10-21T20:23 to 2026-10-28T20:23 has two), sees the new word, then changes it back. No code change unless the new word does not print.

**Files:**
- `scripts/hierarchies/worker_owners.py`, `scripts/hierarchies/test_worker_owners.py` — output order, exit status, `--full`
- `scripts/hierarchies/push_audit.py`, `scripts/hierarchies/test_push_audit.py` — summary and `--full`

**Workers:** `1 writer`
- `impl` — all four files

**Constraints from prior phases:** shared log helpers live in `scripts/hierarchies/log_records.py`; `push_audit.py` never imports `measure.py`. Session names come only from `config/hierarchies/sessions.toml`.

**Acceptance gate:** unit tests green, with tests that pin: an empty roster file gives one source-problem line, worker rows from the other files, Totals as the last line and exit 0; the default worker report prints one timer line and `--full` prints each timer; `end` still ends nothing while a source problem exists; the default push audit prints the summary with the same totals as `--full` and no table rows. Per-slot `basedpyright` ends `0 errors, 0 warnings, 0 notes`; merge gate green. Live on natedev: `worker_owners.py report` exits 0 and its last line starts `Totals:`; `push_audit.py report` over the Baseline window prints at most six lines and names 17 of 64. **Change-Review rows** (new, replacing C443, C446, C447): one per item above, each with a full command a person can paste.

### Phase 9 — An attached production's earlier change rows reach /next and the count, under their own names  · status: todo
**Depends on:** Phase 8 (showrunner request 2026-10-29: a defect in nested showrunners, fixed at its cause).

#### Work Order

**Goal:** when a production attaches under another (sdlc under natedev), the open rows on its own earlier list still show in `/next` and in the For-you count, named `sdlc/C<n>` as before.

**Hana link:** indirect. Changes that passed the showrunner's check but never reach the user's review stall at the last step.

**Spec:**
- Cause: `change_review.list_path` names the list for the top-level showrunner while it runs, so `scope_list`/`scope_rows` read only natedev's list after sdlc attached. sdlc's own list (`~/rust/hanadocs/change-review/change-review - sdlc.md`) still holds open rows that passed the vet (C9, C16, C17, C22, C23, C25, C26, C29, C32, C37, C41) that no command shows.
- An attached production's queue reads its own earlier list as well as its root's list. A row from its own list with no Production cell belongs to that production. Each row keeps its list's name and number (`sdlc/C9`, `natedev/C434`); nothing is renumbered or copied between lists.
- `next_item.py show --kind change` with `--order oldest|newest|urgent`, `move-to-end`, `--all-below` and `footer_counts.py`'s For-you count all read the same merged queue. A list that cannot be read stays one stderr line, not a stop.
- A production that never had its own list behaves as today.

**Files:**
- `scripts/production/change_review.py` — `scope_list`, `scope_rows` (the list set of a production)
- `scripts/production/next_item.py` — the change queue and its pin names
- `scripts/production/footer_counts.py` — the For-you count
- `scripts/production/test_change_review.py`, `scripts/production/test_next_item.py` — tests

**Workers:** `1 writer`
- `impl` — all files

**Acceptance gate:** unit tests green, with a test that pins an attached production's own-list rows in `show`, `move-to-end` and the count with their own names; per-slot `basedpyright` ends `0 errors, 0 warnings, 0 notes`; merge gate green; live on natedev, `next_item.py show --kind change --order oldest --production <sdlc doc>` shows `sdlc/C9`. **Also touches:** `scripts/production/` (owners: builds and enh-showrunner units); name them in the checkpoint notice.
