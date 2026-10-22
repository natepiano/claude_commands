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
- **A flat organization** (user, 2026-10-20, via the showrunner: "I want a flat organization"): top-level showrunners are peers. Today and soon: build-followups (natedev), remove-running-section (cargo-liner), tool-based-ui (hana), sdlc, and the chief designer (`astra-designer`) and chief reviewer (`astra-reviewer`), each a top-level showrunner (user, 2026-10-20, in `docs/plans/sdlc-production.md` on branch `build-followups-sdlc`). Every candidate counts all of them.
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

### Phase 2 — The design doc recommends the best hierarchy and the steps to reach it  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** the design doc compares at least three hierarchies against the constraints and the baseline, recommends one, and splits the change into small steps, each with the measure it moves.

**Spec:**
- Section **Candidates.** At least three, each described by its roles, who decides priority, how work is admitted, how the user is reached, and how capacity (build slots, accounts, workers) is shared. Seeds, not limits:
  - **Today's tree, repaired:** same roles; fix the measured friction (finished units that keep sessions, leaked workers, timers on on-demand productions, queues that grow faster than the user drains them).
  - **Product-led, flat:** showrunners stay peers, but the ranked Hana backlog decides what every production admits; each non-Hana production supplies Hana, and each of its units names the Hana issue or the measured Hana cost (build minutes, screenshot minutes, review time) it cuts.
  - **Capacity-led:** productions stay as they are; one owner shares build slots, accounts and workers by issue rank across all productions.
- Every candidate places sdlc, the chief designer and the chief reviewer as top-level showrunners, and keeps the organization flat (Decisions). A candidate that puts a showrunner above another breaks a `user` constraint.
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

### Phase 3 — Build the first approved step  · status: todo
**Depends on:** Phase 2

#### Work Order

**Pending decision: which step the user approves first**

Actual problem:
Phase 2 ends with a recommendation and numbered steps in `docs/plans/build-followups-hierarchies-design.md`; role and responsibility changes are the user's (memory `process-redesigns-are-questions`).

What exists now:
- No step is approved. The files a step changes (for example `commands/showrunner/produce.md`, `commands/unit/direct.md`, `docs/production_format.md`, `scripts/production/`) have owners in the production doc's Units and Hub files tables.

What should change:
- Phase 2's Step 1, as the user approves or changes it, becomes this Work Order's Spec, Files, Workers and Acceptance gate, with the measure it must move.

Recommendation:
Step 1 from the design doc's Steps section.

**Goal:** the approved step is live on `~/.claude` main.

**Spec:** set by the decision above.

**Files:**
- `docs/plans/build-followups-hierarchies-design.md` — mark the approved step as built in Steps. The step's own files are set by the decision above; a file another unit owns is edited after the showrunner settles it.

**Workers:** set by the decision above.

**Constraints from prior phases:** the design doc's Steps section; the Phase 1 baseline.

**Acceptance gate:** set by the decision above, and the merge gate (Delegation Context → Test) green.

### Phase 4 — Measure the first step and propose the next  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** the design doc shows whether the first step moved its measure, and the next step goes to the user.

**Spec:**
- `measure.py compare --before <baseline window> --after <window from the step's merge>`: the same tables as `baseline`, each row with before, after and the change. The after window is at least 3 days, or the step's own window if the doc names one.
- Design doc section **Results**: per step, the expected and the measured change, and keep or undo. A step whose measure did not move gets an undo recommendation.
- Update **Steps**: re-rank the remaining steps by what the results show.
- After this phase the unit stays as the hierarchy consultant: each next step becomes a new phase after the user approves it, built and measured the same way.

**Files:**
- `scripts/hierarchies/measure.py`, `scripts/hierarchies/test_measure.py` — `compare`
- `docs/plans/build-followups-hierarchies-design.md` — Results, Steps

**Workers:** `1 writer + 1 tester`
- `impl` — `scripts/hierarchies/measure.py` and the design doc
- `test` — `scripts/hierarchies/test_measure.py`: `compare` on fixtures with known before and after values

**Constraints from prior phases:**
- `measure.py baseline [--since DATE|DATETIME] [--until DATE|DATETIME]` exists (date-time form from Phase 2): default window the 7 days to now, end excluded. Each measure is a function over typed domain records built by read-only loaders; `compare` runs the same functions on both windows and does not add new loaders.
- A missing or corrupt source prints a source-problem row instead of failing; `compare` keeps that row in each window and shows no change for it.
- Rows with no recording source (phase wait kinds, issue activation times, question answer times) print `not recorded`; `compare` prints them the same way, unless the Phase 3 step added the record.
- Items waiting on the user come from showrunner/outstanding through `dailies_render.read_outstanding`.
- The step built in Phase 3 and the measure it should move.

**Acceptance gate:** unit tests green; `basedpyright scripts/hierarchies` ends `0 errors, 0 warnings, 0 notes`; the merge gate (Delegation Context → Test) green; the doc's Results row for Step 1 holds real before and after numbers. At phase end the unit director sends the showrunner the Results and the next step as one question for the user.
