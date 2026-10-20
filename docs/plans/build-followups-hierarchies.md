# hierarchies

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready; long-running.** Find the agent hierarchy that delivers Hana fastest under the user's constraints: measure today's hierarchy, collect the constraints, propose the best hierarchy with a step-by-step change to it, then build and measure one approved step at a time.

> **Production: build-followups** — unit `hierarchies-unit`; production doc `docs/plans/build-followups-production.md`

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

#### Work Order

**Goal:** `measure.py baseline` prints the hierarchy's delivery numbers on real data, and the design doc holds the constraints, the map and that baseline.

**Spec:**
- `scripts/hierarchies/measure.py baseline [--since DATE] [--until DATE]` (default window: the last 7 days). Each measure is one function that takes parsed records and returns a typed result, so tests feed fixtures. Output: one aligned table per group, each row with its window and n.
  1. **Capacity on Hana.** Live units per production and repository (from `showrunners.py list` and `units_table.py`), split Hana (`~/rust/hana_catalyst` worktrees) or other. Workers by worktree from the `codex_mesh.py` roster, with the count whose director is not running. Build and test minutes by repository from the buildlog index. Tokens by repository from the usage records where they carry a cwd.
  2. **Flow to Hana main.** For hana units in the window: phases done, median and p90 phase time, and the time phases waited (on the user, the showrunner, a gate, or idle) where `progress_history.py` records it. Hanadocs issues on goal `1 - Ship Hana` moved to active and closed in the window, with median active-to-closed time. Where no source records a wait kind, print `not recorded` and name the source that would.
  3. **User attention.** Phone pushes per day and how many went out after a failed review; agent messages per day split by sender kind (agent, automation, user-bound); items waiting on the user by kind (changes, proposals, efficiency items, questions) with the age of the oldest; questions answered in the window with median time to answer.
  4. **Rank coverage.** Of the top 20 ranked open issues on goal 1, how many have a unit; of the live units, how many name an issue.
  - The script reads natedev's records. A table whose source the Mac also keeps (transcripts, message log) says `natedev only`.
- The design doc `docs/plans/build-followups-hierarchies-design.md`, sections:
  - **Constraints.** Every constraint on how the hierarchy works, one line each, each with its source (user words with date, memory name, CLAUDE.md, `decision_criteria.md`, a production doc). At least: the user's attention and how they want to be reached (on demand, no forms, one plain question, STE, EDT); what only the user decides; build capacity on natedev and the Mac; Claude and Codex account quotas and moves; model choice for judgment roles; worker lifetime; the three gods and the decision economy. Mark each `user` or `derived`; a derived constraint names the user words it comes from.
  - **Map.** Each role, the file that defines it, what it does, who it reports to, and each channel with sender, receiver and script. It includes sdlc and the two guild heads as planned, marked planned.
  - **Baseline.** The `measure.py baseline` output for the 7 days to the run, with the command and its window.
  - **Questions for the user.** Only what the constraints need from the user: a constraint the sources do not settle, or two that conflict. Each one plain line with its recommended answer.

**Files:**
- `scripts/hierarchies/measure.py` — new
- `scripts/hierarchies/test_measure.py` — new
- `docs/plans/build-followups-hierarchies-design.md` — new
- `pyrightconfig.json` — one `executionEnvironments` entry for `scripts/hierarchies` with the `extraPaths` it imports

**Workers:** `2 writers` — the script and the design doc are disjoint.
- `impl` — `scripts/hierarchies/measure.py`, `scripts/hierarchies/test_measure.py`; hub: `pyrightconfig.json` (one entry; another unit may also add one)
- `test` — opens as impl — the design doc's Constraints, Map and Questions sections; it fills Baseline from `impl`'s output when the script runs

**Constraints from prior phases:** none.

**Acceptance gate:** `python3 -m unittest discover -s scripts/hierarchies -p 'test_*.py'` green; `basedpyright scripts/hierarchies` ends `0 errors, 0 warnings, 0 notes`; the merge gate (Delegation Context → Test) green; `measure.py baseline` runs on real data on natedev in under 60 s and its output is in the doc; every constraint line names a source. At phase end the unit director sends the showrunner the Questions section as one message for the user.

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

**Workers:** `1 writer + 1 tester` — nothing splits: one doc. `impl` takes it.
- `impl` — `docs/plans/build-followups-hierarchies-design.md`
- `test` — opens as impl — re-runs `measure.py baseline` for the doc's Comparison window and checks each number the doc cites against it; it writes no doc text

**Constraints from prior phases:** the constraints, the map, the baseline and the user's answers to the Phase 1 questions are in the design doc. Rank 1 of the user's goals is `1 - Ship Hana`.

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

**Files:** set by the decision above. A file another unit owns is edited after the showrunner settles it.

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

**Constraints from prior phases:** `measure.py baseline` and its measure functions from Phase 1; the step built in Phase 3 and the measure it should move.

**Acceptance gate:** unit tests green; `basedpyright scripts/hierarchies` ends `0 errors, 0 warnings, 0 notes`; the merge gate (Delegation Context → Test) green; the doc's Results row for Step 1 holds real before and after numbers. At phase end the unit director sends the showrunner the Results and the next step as one question for the user.
