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
  - `scripts/hierarchies/` — `measure.py` and `test_measure.py` (new)
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
**Depends on:** Phase 1

#### Work Order

**Goal:** the design doc compares at least three hierarchies against the constraints and the baseline, recommends one, and splits the change into small steps, each with the measure it moves.

**Spec:**
- Section **Candidates.** At least three, each described by its roles, who decides priority, how work is admitted, how the user is reached, and how capacity (build slots, accounts, workers) is shared. Seeds, not limits:
  - **Today's tree, repaired:** same roles; fix the measured friction (finished units that keep sessions, leaked workers, timers on on-demand productions, queues that grow faster than the user drains them).
  - **Product-led, flat:** showrunners stay peers, but the ranked Hana backlog decides what every production admits; each non-Hana production supplies Hana, and each of its units names the Hana issue or the measured Hana cost (build minutes, screenshot minutes, review time) it cuts.
  - **Capacity-led:** productions stay as they are; one owner shares build slots, accounts and workers by issue rank across all productions.
- Every candidate places sdlc as a top-level showrunner and guilds as its child (the user, 2026-10-28), with the four guild heads as units of guilds, and keeps the organization flat (Decisions). A candidate that puts a showrunner above another breaks a `user` constraint.
- Section **Comparison.** One table: each candidate scored on each Phase 1 measure (expected direction and size, with the reason) and each constraint (kept or broken). A candidate that breaks a `user` constraint is out.
- Section **Recommendation.** The best candidate and why, in at most ten lines.
- Section **Steps.** The change from today to the recommendation as numbered steps. Each step: what changes (files and roles), the measure it should move and by how much, how to undo it, and whether it needs the user (every role, promotion, rename or responsibility change does). Step 1 is the one with the largest expected effect on Hana delivery per unit of work.
- Before the gate, the unit director runs `/adversarial` on the recommendation against the runner-up and `/astra_review` on the doc, and folds their findings into the doc.

**Files:**
- `docs/plans/build-followups-hierarchies-design.md` — new sections above
- `scripts/hierarchies/measure.py`, `scripts/hierarchies/test_measure.py` — review fixes to the Baseline measures: goal-1 closures split by project with declines left out, Hana merges per week, rank coverage of open issues with live units and a score-weighted ratio, date-time window flags

**Workers:** `2 writers` — one doc and one module. `impl` takes the doc; `test` has no test lane, so it opens as impl, checks numbers, and then fixes the measures.
- `impl` — `docs/plans/build-followups-hierarchies-design.md`
- `test` — opens as impl — re-runs `measure.py baseline` for the doc's Comparison window and checks each number the doc cites against it; then fixes the measure rows the reviews found wrong in `scripts/hierarchies/measure.py` and `scripts/hierarchies/test_measure.py`, re-runs the Baseline, and sends `impl` the new rows

**Constraints from prior phases:**
- The design doc holds Constraints C01–C50 (each marked `user` or `derived`, with its source), the Map, the Baseline (run 2026-10-20 17:24 EDT, 7 days, natedev) and Questions Q1–Q5. Rank 1 of the user's goals is `1 - Ship Hana`.
- Baseline facts the candidates answer: Hana has 11 of 41 live units (27%) and 66 of 187 workers, but 80% of build and check minutes, 82% of test minutes and 41% of tokens; 51 workers record no cwd and have no running director; 598 items wait on the user (315 changes, 161 proposals, 121 efficiency items, 1 question).
- No source records phase wait kinds, issue activation times or question answer times; those rows print `not recorded`. A candidate that needs one of them to show its effect names the record it adds.
- Q1–Q5 went to the user through the showrunner on 2026-10-20 at 17:43 EDT. Write the user's answers into the doc's Questions section as they arrive. While an answer is open, each candidate states its result under the recommended answer and under the other answer, and the Recommendation names which answers it rests on.
- Re-run the baseline with `~/.claude/scripts/lib/py scripts/hierarchies/measure.py baseline` from the worktree root (about 23 s).

**Acceptance gate:** the doc has the four sections; each candidate has a score on every measure and constraint; each step names its measure, its size and how to undo it; the adversarial and astra findings are folded in. At phase end the unit director sends the showrunner the Recommendation and Step 1 as one question for the user.

### Phase 3 — Each user push after a failed text check names its approved exception, and unapproved paths are fixed  · status: todo
**Depends on:** Phase 2

#### Work Order

**Goal:** design Step 1 (HI6, approved 2026-10-28) is live: every send to the user that passes a failed text check is classified as an approved exception or an unapproved path, and each unapproved path is fixed.

**Hana link:** indirect. Each unapproved push takes the user's attention from Hana decisions.

**Spec:**
- `scripts/hierarchies/push_audit.py report [--since DATE|DATETIME] [--until DATE|DATETIME]`: read `~/.local/state/notify/pushover.jsonl` and `review.jsonl` read-only; for each user push after a failed text check, print the sender, the text id or key, and the class: `ApprovedPriorityTwo`, `ApprovedRegisteredText` (the exceptions in `scripts/notify/pushover.py:316-319`), or `Unapproved` with the code path that sent it. Totals per showrunner, with window and n.
- Trace each `Unapproved` class to its sending path (`scripts/notify/review.py`, `scripts/notify/pushover.py`, `scripts/message/send.py`, `commands/showrunner/add_unit.md`, `scripts/production/add_unit.py`). Fix only paths confirmed as not approved, where the defect lives. Do not change an exception: a change to one is its own question for the user.
- The showrunner duty (run the report, record which exception each send uses) goes to roles-unit through the sdlc showrunner as a text proposal; this phase does not edit `commands/showrunner/produce.md`.

**Files:**
- `scripts/hierarchies/push_audit.py`, `scripts/hierarchies/test_push_audit.py` — new
- `scripts/notify/` fixes — messaging-unit's files; this unit edits them (showrunner ruling 2026-10-28); the notice carries an also-touches line per build-followups owner
- `scripts/message/send.py` fixes — shutdown-unit's file; `scripts/production/add_unit.py` and `commands/showrunner/add_unit.md` fixes — enh-showrunner-unit's files (build-followups production doc). Same rule as `scripts/notify/` (unit director's call, 2026-10-28, extending ruling 1 to every owner a confirmed fix reaches): this unit edits the file where the defect lives, tells the sdlc showrunner which owner's file before the edit, and the notice carries an also-touches line for each owner, trial-merged against its tip. Only files with a confirmed `Unapproved` path are edited.
- `docs/plans/build-followups-hierarchies-design.md` — Step 1 result

**Workers:** `1 writer + 1 tester`
- `impl` — `push_audit.py`, the confirmed fixes
- `test` — `test_push_audit.py`: fixtures for each class, a priority-2 send, a registered text, an unapproved send, a missing log

**Constraints from prior phases:** Baseline **User attention**: 64 pushes, 16 after a failed review, n=64 (2026-10-13 17:24 to 2026-10-20 17:24 EDT). The 16 are not 16 faults. The audit reads only; it sends nothing (C49).

**Acceptance gate:** unit tests green; per-slot `basedpyright` ends `0 errors, 0 warnings, 0 notes`; merge gate green; the report on the Baseline window and on the last 7 days shows each send's class; zero `Unapproved` sends after the fixes, or each one left names its open question. **Measure:** unapproved sends per week, from 16 after-failed-review pushes in the Baseline (n=64). **Review point (C53):** re-run the report 2 weeks after the merge; Phase 6 keeps or drops the duty on it.

### Phase 4 — The measure reads wait records and merge receipts, and the 4-week experiment starts  · status: todo
**Depends on:** Phase 2; and a wait on sdlc phases-unit (approved by the sdlc showrunner 2026-10-28; the row for the sdlc production doc's Gates table goes to the sdlc showrunner with the Phase 2 notice):
- **Supplier:** phases-unit's HI7 phase, the phase it plans right after its phase 3 (phases-unit, 2026-10-28).
- **Record contract:** the shape sent to phases-unit on 2026-10-28, given under Spec below. phases-unit picks the file and the writer and sends both, with the commit, when its phase lands.
- **Clears when:** that phase's checkpoint is merged to `origin/sdlc` and on `~/.claude` main, its message names the record file and commit, and the file holds at least one wait record and one merge receipt written by a real phase. Until then this phase does not start.

#### Work Order

**Goal:** design Step 2 (HI7, approved as a 4-week experiment): `measure.py` reads the wait records and merge receipts that phases-unit builds, and the doc names the experiment's start and review date.

**Hana link:** direct. The records show which waits slow Hana phases.

**Spec:**
- The record shape went to phases-unit on 2026-10-28 (showrunner ruling 3) the design needs: per wait, phase, owner, UTC start and end, and one kind (`WaitingForQuotaRefusal`, `WaitingForMemoryOrSlot`, `WaitingForMergeGate`, `WaitingForUser`, `WaitingForSupplier`, `ReadyWithoutCapacity`); per merge, a receipt (project, issue or plan phase, commit, target branch, merge time). The duty text goes to roles-unit. phases-unit picks the file and writer; this phase adapts to it.
- `measure.py baseline` fills the four `Hana wait /` rows and a receipts row from those records: minutes by kind with n, `HistoryNotRecorded` for a window with no records (never zero), and `ReceiptMissing` per merge with no receipt. Readers are read-only typed loaders; a corrupt source prints a source-problem row.
- Design doc: the experiment's start (first records on `~/.claude` main) and its review date, four weeks later.

**Files:**
- `scripts/hierarchies/measure.py`, `scripts/hierarchies/test_measure.py`
- `docs/plans/build-followups-hierarchies-design.md` — Step 2 start and review date

**Workers:** `1 writer + 1 tester`
- `impl` — `measure.py` and the doc
- `test` — `test_measure.py`: fixtures for each wait kind, a window with no records, a merge with and without a receipt

**Constraints from prior phases:** the Baseline rows print `not recorded` today; `--since` and `--until` take dates and date-times.

**Acceptance gate:** unit tests green; per-slot `basedpyright` ends `0 errors, 0 warnings, 0 notes`; merge gate green; `measure.py baseline` over the first records prints real wait and receipt rows. **Measure:** share of Hana phase time with a recorded wait kind, and share of merges with a receipt. **Review point (C53):** four weeks after the first records land; Phase 6 keeps or drops the step on it.

### Phase 5 — A run end ends only workers whose unit director is gone or whose owner cannot be found  · status: todo
**Depends on:** Phase 2

#### Work Order

**Goal:** design Step 4 (HI9, approved with the user's rule): workers live as long as their unit director, across runs, and are reused by name; a check ends only workers with no live director or no known owner, and each showrunner removes timers nobody asked for.

**Hana link:** indirect. Workers with no owner hold quota and memory on the accounts and the machine Hana shares.

**Spec:**
- `scripts/hierarchies/worker_owners.py report`: list each Codex worker with its recorded run, unit and director session, and one class: `OwnedLive`, `PooledReviewer`, `DirectorGone`, `OwnerUnknown`. Read-only.
- `worker_owners.py recover`: for each `OwnerUnknown` worker, try provenance recovery first (shutdown history records, the message log, launcher records naming its thread id); a recovered worker moves to its real class. The 2026-10-28 run-folder deletions made live units' workers look ownerless, this unit's included.
- `worker_owners.py end --class DirectorGone|OwnerUnknown [--dry-run]`: end only those, through `codex_mesh.py end`, and log each one with its evidence. `OwnerUnknown` is ended only after recovery failed for that worker. Never an `OwnedLive` worker, a pooled reviewer, or a named worker of a live unit director. **The first run reports and ends nothing** (showrunner, 2026-10-28).
- A worker survives its run's end: the change to `end_session.sh` and `codex_mesh.py` that keeps a live director's workers goes where the defect lives (`scripts/agents/` is owned by build-followups models-unit; this unit edits it under showrunner ruling 1, 2026-10-28). The <LongLivedWorkers/> text change ("run-scoped", "`end_session.sh` ends them") goes to phases-unit; the duty goes to roles-unit.
- Timers: list notifier registrations (`scripts/message/notifier.sh`) with whether a user or showrunner asked for each; the showrunner removes the rest.

**Files:**
- `scripts/hierarchies/worker_owners.py`, `scripts/hierarchies/test_worker_owners.py` — new
- `scripts/agents/codex_mesh.py`, `scripts/direct/end_session.sh` — the keep rule (models-unit's files; this unit edits them, showrunner ruling 2026-10-28; the notice carries an also-touches line per build-followups owner)
- `docs/plans/build-followups-hierarchies-design.md` — Step 4 result

**Workers:** `1 writer + 1 tester`
- `impl` — `worker_owners.py`, the keep rule
- `test` — `test_worker_owners.py`: fixtures for each class, a pooled reviewer, a director that restarted on another account

**Constraints from prior phases:** Baseline **Capacity on Hana**: 51 workers with no recorded cwd and no running director, n=51. A paused worker of a live director is `OwnedLive`. The 2026-10-28 run-folder loss (a paused run's roster deleted) is one case the report must name, not hide.

**Acceptance gate:** unit tests green; per-slot `basedpyright` ends `0 errors, 0 warnings, 0 notes`; merge gate green; `report` on the live roster names an owner or a class for every worker; the first run ends no worker. **Measure:** Baseline 51 workers with no known owner; target zero after recovery. **Review point (C53):** re-run 2 weeks after the merge; Phase 6 keeps or drops the step on it.

### Phase 6 — The four-week review: which waits slow Hana, which links helped, and which trial steps stay  · status: todo
**Depends on:** the checkpoints of Phases 3, 4 and 5, and the three review records: the Phase 3 and Phase 5 re-runs 2 weeks after each merge, recorded in the design doc under Steps 1 and 4, and four weeks of Phase 4 records. The phase starts on the latest of these dates, never before.

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

**Workers:** `1 writer + 1 tester`
- `impl` — `measure.py` and the doc
- `test` — `test_measure.py`: `compare` on fixtures with known before and after values

**Acceptance gate:** unit tests green; per-slot `basedpyright` ends `0 errors, 0 warnings, 0 notes`; merge gate green; Results holds real numbers for each step, judged on the measure and review point each of Phases 3–5 names. **Measure and review point:** this phase is the review point for Steps 1, 2 and 4. At phase end the unit director sends the showrunner the Results and the keep-or-drop list as one question for the user.
