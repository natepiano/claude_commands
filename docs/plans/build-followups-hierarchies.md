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

### Phase 4 — The measure reads wait records and merge receipts, and the 4-week experiment starts  · status: done

#### As-built

- `measure.py` defines `WaitKind` with six kinds: `WaitingForQuotaRefusal`, `WaitingForMemoryOrSlot`, `WaitingForMergeGate`, `WaitingForUser`, `WaitingForSupplier`, `ReadyWithoutCapacity`.
- `read_wait_history` runs `progress_history.py waits --session-dir <S> [--since] [--until]` and parses the output into `CompletedWait`, `UnknownEndedWait`, `CoveredWaitHistory`, `UncertainWaitCoverage`, `PhaseWaitCoverage`, `HistoryNotRecorded`, `WaitCauseNotRecorded`, `WaitPhaseNotRecorded`, `ProductionHandoff`, `RecordedWaitOwner`/`WaitOwnerNotRecorded`, `NamedSupplier`/`NoNamedSupplier`, `RecordedWaitReason`/`WaitReasonNotRecorded` and `AttributionMiss`. External waits come from `wait_attribution.jsonl` and the run files. A production handoff wait has no phase.
- `hana_wait_rows` prints one `Hana wait / <kind>` row per kind (minutes, n), unknown-ended rows with n, and `Hana wait / share of phase time with a recorded kind`. An unclosed wait stays out of every completed-duration total. Uncertain coverage prints as its own rows with reason and candidates, never as a covered zero.
- `parse_receipt`, `read_receipts`, `receipt_for_merge` and `receipt_rows` print `merge receipts / all projects`, `Hana merge receipts / <branch>` (k/n), and one `Hana merge / ReceiptMissing` row per Hana merge with no receipt.
- An external wait record with no `wait_id` prints a source-problem row, as does a corrupt source.
- Design doc Step 2 records the experiment: first records 2026-10-29, review 2026-11-26.

**Files:**
- `scripts/hierarchies/measure.py` — wait and receipt loaders, types and rows
- `scripts/hierarchies/test_measure.py` — tests for each kind, each missing state, uncertain coverage, unclosed waits and phase-less handoff waits
- `docs/plans/build-followups-hierarchies-design.md` — Step 1 numbers, Step 2 reader and experiment dates

**Binds later work:** missing history never reads as zero. The summary keeps five lines; the wait and receipt rows print under `--full`. Readers import shared helpers from `scripts/hierarchies/log_records.py`; `push_audit.py` never imports `measure.py`. The experiment start waits on a supplier holder field: a Hana wait counts as non-Hana-caused only when a record names the non-Hana holder of the account, slot or memory, and the supplier records do not carry that holder yet.

**Gotchas:**
- The supplier projection keeps only the worktree name for a phase (no `plan_doc`), so a retired Hana phase is not recognized.
- `progress_history.py waits` drops a wait row with no `wait_id` and does not count it.
- Live numbers, 2026-10-22..29: share 0.2% partial (`HistoryNotRecorded`=79); `WaitingForMemoryOrSlot` 148.5 min, n=13; 11 unknown-ended; 4 receipts across all projects; 0/25 Hana merges with a receipt.

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
**Depends on:** the checkpoints of Phases 3, 4 and 5, and the three review records: the Phase 3 and Phase 5 re-runs, which the unit director runs on 2026-11-12 (two weeks after both merged into sdlc as 3c5dea67 on 2026-10-29) and records in the design doc under Steps 1 and 4 (the push-audit re-run also confirms that `measure.py` and `push_audit.py` agree; each re-run record holds the report's source-problem lines, because both reports exit 0 with source problems), Phase 12's checkpoint (merged before the 2026-11-12 re-run), and four weeks of Phase 4 records that carry the holder field (Phase 10). The phase starts on the latest of these dates, never before.

#### Work Order

**Goal:** design Step 5: the doc reports which waits slow Hana (HI7), which Hana links helped (HI2), and keep or drop for each trial step (HI1).

**Hana link:** direct. The review keeps a step only by its effect on Hana.

**Spec:**
- `measure.py compare --before <Baseline window> --after <the four-week window>`: the same rows for both windows, each with before, after and the change; `not recorded` rows stay `not recorded`. The windows differ in length (7 days and 28 days), so each count row (merges, closures, pushes, waits) prints a rate per week, never a raw total, and the change is between the two rates. A row whose source covers only part of a window prints its covered days beside the rate and is marked partial.
- Set quota and memory waits beside same-hour non-Hana tokens on the same role account and non-Hana build memory on natedev. If the records show non-Hana work slowing Hana, bring a new proposal (HI10); there is no size threshold. Records show it only when a record names a non-Hana cause for a Hana wait: a `WaitingForSupplier` wait whose record names non-Hana work, or a quota or memory wait whose record, or the slot or hold record that covers the whole wait, names a non-Hana holder of that account, slot or memory. Load in the same hour is context, never proof. A wait with no named cause is reported as cause not recorded, with its minutes and n, and does not count. Report each wait with a named non-Hana cause with its minutes and n. The holder comes from Phase 10's holder history: a wait counts as non-Hana-caused only for the minutes a named non-Hana holder covers; a wait whose holder is named for part of its time reports the named and not-named minutes apart.
- Design doc section **Results**: per step, the measure, its change, keep or drop. The link records (Step 3) set beside Hana merges and closures.

**Files:**
- `scripts/hierarchies/measure.py`, `scripts/hierarchies/test_measure.py` — `compare`
- `scripts/hierarchies/push_audit.py`, `scripts/hierarchies/test_push_audit.py` — `audit()` per window
- `docs/plans/build-followups-hierarchies-design.md` — Results

**Spec additions (architect review after Phase 7, 2026-10-29):**
- Coverage: a rate per week needs the covered days of each source. Give recorded coverage, an unavailable source and unknown coverage distinct domain types (no bare `None`); event timestamps alone never prove continuous coverage. Fixtures: empty but covered, partly covered, missing, corrupt.
- Compare: load the notification sources once and call `push_audit.audit()` per window, so reviews and silence records outside a window still pair. The failed-check count is `len(entries)`; the attention row's n is delivered pushes. Fixtures: a review across a window boundary, a delayed delivery, a rewrite, a held `nate.adhoc` review with no delivery.
- A missing or malformed `config/hierarchies/sessions.toml` stays visible in `compare` as a source problem naming the file; valid delivery counts never become zero and an attribution failure is never missing delivery history.

**Spec additions (architect review after Phase 9, 2026-10-29):**
- Cause evidence: keep apart, as distinct types, a known wait kind, a named supplier, a named holder, and proven non-Hana involvement; no bare `None` for any outcome. Fixtures: a known quota wait with no holder, and a named supplier whose project cannot be found.
- Hourly inputs: `read_tokens` sums by repository and drops the time and the role, so the same-hour token row needs a per-hour, per-account view that keeps them; `BuildStep` has no memory field, so non-Hana build memory needs a named source, or the row prints `not recorded` with its reason. Fixtures: an account change inside one hour, a retired holder, a missing attribution, a build across two hours.
- Present-state rows (`pending_attention`, capacity, rank coverage) read the state now, and the design doc says their history cannot be rebuilt. `compare` uses the recorded Baseline snapshot for the before side, prints `comparison unavailable` where none exists, and gives a per-week rate only for window event counts. Since Phase 9, the waiting-change count reads earlier lists too; report that visibility change apart from backlog growth.

**Workers:** `1 writer + 1 tester`
- `impl` — `measure.py`, `push_audit.py` and the doc
- `test` — `test_measure.py` and `test_push_audit.py`: `compare` on fixtures with known before and after values, and the fixtures above

**Constraints from prior phases:** shared log helpers live in `scripts/hierarchies/log_records.py`; `measure.py` imports `push_audit.py`, never the reverse; session names come only from `config/hierarchies/sessions.toml`. The wait records come from `progress_history.py waits` (phases-unit 820357a2; holder and origin fields from 49fd1b0f) through the Phase 4 loader and types. Phase 11's repair reads origin fields into `RecordedOrigin` / `OriginNotRecorded`; Phase 10 adds the unresolved-origin row and the holder history, and this review keeps that uncertainty visible. From Phase 8: `measure.py baseline` and `push_audit.py report` print a short summary by default and their tables under `--full`; `compare` prints the same way.

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

### Phase 8 — The worker ownership report and the push audit open with what a person needs  · status: done

#### As-built

- `worker_owners.py report` and `recover` always finish and exit 0 when they print the report. An empty or unreadable source file is a source problem. The report still prints every worker row it can read.
- Text output order: worker rows, notes, source-problem lines, one `Timers: <n> registered, <m> with no recorded request` line, then `Totals:` last with `source_problems=N`. `--full` prints each timer line in place of the one `Timers` line. `--json` is unchanged.
- `worker_owners.py end` exits 1 when a source problem exists or an end fails. A timer source that cannot be read stops all ends.
- `push_audit.py report` prints `report_summary()` by default: four lines (window; delivered pushes and pushes after a failed text check; counts per class ApprovedPriorityTwo / ApprovedRegisteredText / Unapproved; source-problem count and the `--full` hint). `--full` prints the summary, then the tables. Both forms show the same numbers.
- Live Baseline window: 64 delivered, 17 after a failed text check, classes 1 / 16 / 0.

**Files:**
- `scripts/hierarchies/worker_owners.py`, `scripts/hierarchies/test_worker_owners.py` — output order, exit status, `--full` timers
- `scripts/hierarchies/push_audit.py`, `scripts/hierarchies/test_push_audit.py` — `report_summary()` and `--full` tables

**Binds later work:** reports print a short summary by default and their tables under `--full`; a new report or compare follows the same form. `push_audit.py` never imports `measure.py`; shared log helpers live in `scripts/hierarchies/log_records.py`.

### Phase 9 — An attached production's earlier change rows reach /next and the count, under their own names  · status: done
**Depends on:** Phase 8 (showrunner request 2026-10-29: a defect in nested showrunners, fixed at its cause).

#### As-built

- `change_review.scope_lists(path, doc, below)` returns one `ProductionReviewList(path, default_production)` per list: the current list (default production: the root) and the own list of each scoped production that has one (default production: that production). Each path occurs once. When the root showrunner is stopped and the current list is a scoped production's own list, that production owns its rows with no Production cell.
- `scoped_review_entries` reads and ranks each list on its own and returns `ScopedReviewEntry(review_list, entry)`. A list that cannot be read, or that holds a bad Urgency cell, prints one `change review: <path>: <error>` line on stderr and drops out; the other lists stay. `scope_rows` returns the rows of these entries.
- `scope_entries` gives row IDs: a current-list row keeps its plain ID (`C434`); a row from another list carries the list name (`sdlc/C9`), and so does its `after` prerequisite. Nothing is renumbered or copied between lists.
- `next_item.change_queue` (`show --kind change` with each `--order`, `move-to-end`, `--all-below`) reads `scope_entries`. `footer_counts` counts `scope_rows` and watches every path from `scope_lists`. Live on natedev, `/next` showed `sdlc/C9` first and the For-you count went from 11 to 22.
- `change_review.py edit|verdict|mark-important --row` takes the ID as `/next` prints it. `_row_option` returns `ReviewRowTarget(path, row)`: `C434` selects the current list; `sdlc/C9` selects `list_file("sdlc")`. A list-named ID that differs from an explicit `--list` is refused before any write. `commands/next.md` and `commands/change_review.md` say to pass the ID as `/next` shows it.
- `change_review.py show` still reads only the current list.
- A production that never had its own list behaves as before.

**Files:**
- `scripts/production/change_review.py` — `ProductionReviewList`, `ScopedReviewEntry`, `scope_lists`, `scoped_review_entries`, `scope_rows`, `scope_entries`, `ReviewRowTarget`, `_row_option`
- `scripts/production/next_item.py` — the change queue over all scoped lists
- `scripts/production/footer_counts.py` — the For-you count and its watched sources
- `scripts/production/test_change_review.py`, `scripts/production/test_next_item.py` — list-named IDs in show, move-to-end and the count; pins and moved rows; one bad list; the `--list` mismatch refusal
- `commands/next.md`, `commands/change_review.md` — pass the row ID as `/next` shows it

**Binds later work:** `/change_review shows the same first row as /next, also from an attached production's earlier list` moves `change_review.py show` onto the same merged queue as `/next`.

**Gotchas:** pins and moved-row state use the plain ID of a current-list row. Never put a list name on a current-list key.

**Ruled out:** a list name on every row ID — it orphans saved pins and moved-row state.

### Phase 10 — The measure reads who holds a wait, and the experiment start is set  · status: done
**Depends on:** phases-unit Phase 11 (the wait-holder field and the phase origin fields), merged as 49fd1b0f (sdlc b8ec8a99, 2026-10-29); Phase 11 of this plan (its repair reads the new origin shape). Not at the same time as Phase 12 (both edit `measure.py`).

#### Work Order

**Goal:** each recorded Hana wait says who holds the account, slot or memory it waits on, or says the holder is not recorded; a retired Hana phase keeps its Hana origin; the design doc names the experiment's real start.

**Hana link:** direct. The four-week review counts a Hana wait as non-Hana-caused only when a record names the holder.

**Spec:**
- Read each wait's `holders` from `progress_history.py waits` into a holder history at the loader boundary: the ordered holders seen, each with its time and a `NamedHolder` (resource kind `account`, `slot` or `memory`; the resource; a `SessionHolder` or `UnitHolder`), or one `HolderNotNamed` with its reason. Keep every holder seen, so a holder change during a wait stays visible. Never infer a holder. The loader accepts the new fields; a field or value it does not know is a source problem for that row, never a stop for the report.
- `measure.py baseline --full` shows, per wait kind, the waits and minutes with a named holder for the whole wait, for part of the wait, and with `HolderNotNamed`, each with n.
- Origin: the supplier writes `plan_doc`, `production` and `repository` each as `{"state":"Recorded","value":"<name>"}` or `{"state":"NotRecorded","reason":"<why>"}`; `repository` is the name of the git common directory and `unit` stays the worktree name. Phase 11's repair already reads both shapes, and the old plain text, into typed values at the boundary. Keep that reader; a not-recorded field never discards the report or the other rows. A retired phase whose worktree name no longer carries a unit prefix keeps its `HistoryNotRecorded` row. Fixture: a retired phase with worktree `widget-enhancements` and plan `tool-based-ui-widget.md` beside a covered Hana phase, and a row with not-recorded origin fields beside valid Hana rows.
- A wait row with no `wait_id` that the supplier now counts in `ignored_n` / `wait_without_id_n` is still a source problem, not a zero.
- Rename `WaitCauseNotRecorded` to `WaitKindNotRecorded`: it is built when `wait_kind` is absent and says nothing about cause.
- The reader's checkpoint does not start the experiment. The experiment starts on the first day on which each holder producer (builds' `verify.sh` for slot, memory and hold waits; models-unit's `codex_mesh.py` for quota and capacity waits) has recorded a `NamedHolder`, read live from `waits`. Design doc Step 2 records the start and the review date (four weeks later) as UTC bounds. While a producer has not emitted, Step 2 says `start not set: waiting on <producer>`, and the unit director tells the sdlc showrunner which producer. On 2026-10-29, `verify.sh` (~1073) and `codex_mesh.py` (~3243) still call `start-wait` without holder flags; builds and models-unit own those calls, and sdlc has told them. One wait plus one receipt never sets the start (sdlc, G1): it needs a `NamedHolder` from each producer.
- A phase whose origin fields are all `NotRecorded` and that no worktree or plan path identifies is not dropped: it becomes a typed unresolved-origin outcome, and `baseline --full` prints a row with its run, phase instance and reason (architect review after Phase 11 found it silently dropped, 0.0 min). Mixed-record test: such a phase beside valid Hana rows.
- Row-level recovery: a malformed holder or origin value beside valid Hana rows leaves the valid counts in the final report and names the rejected row; the whole report never falls back to missing history. Test it with mixed fixtures.
- `baseline --full` shows, per wait with a holder history, the wait id, the resource, the session or unit, the observation times and the unnamed intervals; a wait with no recorded end keeps its duration unknown, never zero.
- HI7 acceptance case: one test reads, through the real `progress_history.py waits` and on to the final `baseline --full` report, a producer's named holder, a holder change during one wait, and an unnamed holder. Unknown evidence shows as recorded or not recorded with its reason, never as zero delay.

**Files:**
- `scripts/hierarchies/measure.py`, `scripts/hierarchies/test_measure.py` — holder history type, the unresolved-origin row, the holder rows, the rename (origin conversion shipped in Phase 11)
- `docs/plans/build-followups-hierarchies-design.md` — Step 2 experiment dates

**Workers:** `1 writer + 1 tester`
- `impl` — `measure.py` and the design doc
- `test` — `test_measure.py`: holder named for the whole wait, a holder change during one wait, a wait with a holder for part of its time, `HolderNotNamed` and `HolderNotNamed("recorded before holders")`, not-recorded origin fields, the retired-phase fixture, the missing-id count, and one test that runs the real `progress_history.py waits` on a fixture history file

**Constraints from prior phases:** the supplier schema is sdlc's `docs/plans/sdlc-phases.md` Phase 11, merged as 49fd1b0f: producer flags `--holder-resource-kind`, `--holder-resource` and `--holder-session` or `--holder-unit` on `start-wait`, and `progress_history.py wait-holder --session-dir D --wait-id ID` when the holder changes; `NamedHolder(resource_kind, resource, holder)`, `SessionHolder(session_id)`, `UnitHolder(unit)`, `HolderNotNamed(reason)`, `wait_holder_seen` events, `wait_without_id_n`, and the phase row fields `plan_doc`, `production`, `repository`. Wait kinds, `HistoryNotRecorded`, `UncertainWaitCoverage`, `WaitCauseNotRecorded`, `NamedSupplier`/`NoNamedSupplier` and `RecordedWaitReason`/`WaitReasonNotRecorded` exist in `measure.py`; the summary keeps its five lines and new rows print under `--full`; `push_audit.py` never imports `measure.py`; shared log helpers live in `scripts/hierarchies/log_records.py`. Phase 12 also edits `measure.py`; the two phases never run at the same time. From Phase 11: `PhaseOrigin` fields are `RecordedOrigin | OriginNotRecorded`, read by `origin_field`; `scope_queue` lives in `scripts/lib/urgency.py`. When imports move, the type check also covers the gate's cycle files (`scripts/accounts/accounts.py`, `scripts/hooks/showrunner_footer.py`), or run the merge gate's basedpyright.

**Acceptance gate:** unit tests green, with the fixtures above; per-slot `basedpyright` ends `0 errors, 0 warnings, 0 notes`; merge gate green; live on natedev, `measure.py baseline --full` over a window after the supplier merge prints the holder rows per wait kind. **Change-Review row:** one, with the full command.

### Phase 11 — /change_review shows the same first row as /next, also from an attached production's earlier list  · status: done

#### As-built

- `change_review.change_queue(path: Path, scope: Path | WholeReviewList, below: bool = False) -> ChangeQueue` is the one change queue. `ChangeQueue` holds `name` (the pin name) and `entries`. For a production doc it reads `scope_entries(path, doc, below)` and pins under `queue(path)` for the root, or `<queue>-<slug>` for a lower showrunner. `WholeReviewList.ALL_ROWS` reads every row of an explicit list.
- `next_item.change_queue` and `change_review.py show` both call it. With a production, `show` reads the current and earlier lists, prints the row ID as `/next` does (`C434`, `sdlc/C9`), keeps the same pin, and its `N more to review` count is the queue length. `show --list` with no production reads every row of that list.
- `measure.py` reads origin fields into typed values. A field that is not recorded never marks a phase as Hana.

**Files:**
- `scripts/production/change_review.py` — `ChangeQueue`, `WholeReviewList`, `change_queue`, `show`
- `scripts/production/next_item.py` — `change_queue` wraps the shared queue
- `scripts/lib/urgency.py` — `scope_queue`, `ALL_BELOW`
- `scripts/production/update_registration.py`, `scripts/production/rename_outside.py` — call `urgency.scope_queue`
- `scripts/hierarchies/measure.py` — `RecordedOrigin`, `OriginNotRecorded`, `origin_field`
- `scripts/production/test_change_review.py`, `scripts/production/test_next_item.py`, `scripts/hierarchies/test_measure.py` — tests

**Binds later work:** `PhaseOrigin` fields `plan`, `production` and `repository` are `RecordedOrigin | OriginNotRecorded`. `origin_field(record, key)` reads them for the wait report and phase completion readers. It accepts the supplier's tagged shape (`state` `Recorded` with `value`, or `NotRecorded` with `reason`) and earlier plain text, and raises `ValueError` on any other shape. `scope_queue(name, below)` and `ALL_BELOW` live in `scripts/lib/urgency.py`. `next_item.py` imports `change_review.py`, never the reverse. A pin set by `/next` is the row `/change_review show` shows, and the reverse.

**Gotchas:** the merge gate's basedpyright reports import cycles through function-local imports, and a per-file type check does not. A change that moves an import must type-check the cycle files (`accounts.py`, `showrunner_footer.py`) or run the gate's basedpyright.

### Phase 12 — The push audit judges each push by the exception in force when it went out, and the wait share counts one set of phases  · status: todo
**Depends on:** Phase 4. Merge before the 2026-11-12 re-runs. Not at the same time as Phase 10 (both edit `measure.py`).

#### Work Order

**Goal:** the 2026-11-12 push-audit re-run counts unapproved pushes correctly, and the wait-share row is a share of one phase population.

**Hana link:** direct. Phase 6 keeps or drops trial steps on these two numbers.

**Spec:**
- Exception at delivery time: `push_audit.classify()` returns `ApprovedRegisteredText` for any recorded text id, also a normal-priority `nate.adhoc` delivery after its exception ended. Classify each delivery by the exception in force at its delivery time (the policy's effective boundary is the messaging-unit c73a2d02 merge); keep recorded exception evidence apart from approval. Fixtures: a send before the boundary, one after it, and a held one.
- Wait share: `wait_share_row` sums waits picked by their start time, production hand-offs with no phase included, over phases picked by their finish time, so a wait can add minutes whose phase adds no time, and overlapping waits count the same phase time more than once. Count only waits of the phases in the denominator, count the union of a phase's overlapping waits once, and print hand-offs with no phase as their own row with n. Fixtures: a phase across the window boundary, two waits at the same time in one phase, a hand-off with no phase.

**Files:**
- `scripts/hierarchies/push_audit.py`, `scripts/hierarchies/test_push_audit.py` — `classify`
- `scripts/hierarchies/measure.py`, `scripts/hierarchies/test_measure.py` — `wait_share_row`

**Workers:** `1 writer + 1 tester`
- `impl` — `push_audit.py` and `measure.py`
- `test` — `test_push_audit.py` and `test_measure.py`: the fixtures above

**Constraints from prior phases:** `measure.py` imports `push_audit.py`, never the reverse; shared log helpers live in `scripts/hierarchies/log_records.py`; session names come only from `config/hierarchies/sessions.toml`; `measure.py baseline` and `push_audit.py report` print a short summary and their tables under `--full`. From Phase 11: `PhaseOrigin` fields are `RecordedOrigin | OriginNotRecorded`; from Phase 10: the unresolved-origin row stays visible in the wait share. The policy boundary is the merge of c73a2d02, `dde7ec2e` at 2026-10-29T03:05:59Z. Tests: `python3 -m unittest scripts.hierarchies.test_push_audit scripts.hierarchies.test_measure`.

**Spec additions (architect review after Phase 11):**
- `PhaseCompletion` keeps the run id and phase instance id, so waits match their phase; load the earlier history a phase in the window needs, since the supplier filters waits by start time; join each phase's origin from its `phase_started` record (the supplier does not copy origin fields to `phase_finished`). The boundary test runs through the real supplier reader, with a retired worktree.
- Test a delivery exactly at the boundary, and a review before it whose delivery follows it.

**Acceptance gate:** unit tests green, with the fixtures above; per-slot `basedpyright` ends `0 errors, 0 warnings, 0 notes`; merge gate green; live on natedev, `push_audit.py report` and `measure.py baseline --full` over the Baseline window print the corrected rows, and both commands over a window after the boundary (2026-10-29T03:05:59Z) show the revoked exception applied. **Change-Review row:** one per command, with the full command.
