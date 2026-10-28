# sdlc-phases

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready; long-running.** Own the phase process: measure how a `/unit:direct` phase runs from start to end and how design and documentation docs are made before a plan exists, find where time and tokens go, propose changes, then build and measure one approved change at a time.

> **Production: sdlc** — unit `phases-unit`; production doc `docs/plans/sdlc-production.md`

## Source

2026-10-20 16:47 AST

I think we do need at least a unit director devoted in Nate Dev to managing the phase process.

how we prepare our documentation and design and how the design and documentation process runs

For active process redesigns, as we are currently undergoing with promotions and unit director renames and responsibility changes and process changes, those should come back to me as questions.

## What exists today (measured 2026-10-20 16:45–16:51 AST by the plan author)

- **Phase process files:** `commands/unit/direct.md` (1579 lines), `docs/delegate/*.md` (8 files, 1185 lines), `commands/plan/*.md` (4 files, 1824 lines: `to_phased_plan`, `phase_review`, `shrink`, `to_as_built`), `commands/unit/*.md` helpers (`brief`, `checkpoint`, `ci`, `phase_report`, `eta`, `add_ons`, `report`, `style_review`), `docs/delegate_plan_format.md` (255 lines), `docs/production_format.md` (468 lines).
- **Before a plan exists:** `commands/team_review.md`, `adversarial.md`, `api_review.md`, `module_review.md`, `astra_review.md`, `adhoc_review.md`, `commands/producer/greenlight.md` and `to_production.md` (319 lines), `commands/showrunner/proposal.md`. No repository holds a `*-design.md` doc today; hierarchies-unit starts the first one.
- **Agent time** (`history.py summary`, all 206 run files, 1777 phases): fix 40%, implementation 35%, test 17%, review 5%, other 2%, final (shrink, closeout, phase review, synthesis) 1%; 4564 h of agent time over 701 h elapsed. 197 phases (11%) with 4 or more fix passes hold 75% of fix time. The unit director's own time between passes (compose, synthesize, checkpoint, shrink, phase review) is not a stage in `history.py`.

## Decisions

- **Every change to the phase process is a question for the user** (user, 2026-10-20). This unit measures and proposes; the showrunner brings each proposal to the user; no live command, doc or script that runs phases changes before the user approves it. Measuring and writing the design doc need no approval (plan author).
- **Owner of the phase process files** (production doc, Hub files): this unit owns `commands/unit/direct.md`. guilds-unit (designer and reviewer roles) and fix-rounds-unit (repair round rules) send this unit their edits to it, and this unit merges them into one coherent contract (plan author, from the hub table). It is also a build-followups hub file: an edit goes through cargo-berth's normal flow and the checkpoint notice names the build-followups owner.
- **Scope beside the other units** (production doc, Scope of each unit): this unit owns the steps of a phase and the doc pipeline before a plan. fix-rounds-unit owns repair round counts and their rules; this unit's measure reports fix time as one number and does not study it. guilds-unit owns who designs and who reviews; this unit owns which docs exist, their formats and the commands that turn one into the next. roles-unit owns responsibilities and names; a step that changes either goes to roles-unit through the showrunner. hierarchies-unit (build-followups) owns the structure of productions.
- **One design doc** holds the map, the baseline, the friction, the candidates and the steps: `docs/plans/sdlc-phases-design.md` (plan author). It becomes the as-built at run end.
- **One measure script, run again after each step** (plan author): `scripts/direct/phase_process.py`. It sits in `scripts/direct`, which the merge gate and `basedpyright scripts/direct` already cover, so it needs no new suite or pyright entry. A step counts as done only when its measure moved.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions. This plan measures the phase process and the doc pipeline before it, and changes them one approved step at a time.
- **Project started:** 2026-10-20T20:53:27.319+00:00
- **Stack:** Python 3.13, standard library only; Markdown.
- **Layout:**
  - `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py` — the measure script and its tests (new)
  - `docs/plans/sdlc-phases-design.md` — the design doc (new)
  - read only, outside the repository: `~/.local/state/plan-delegate/runs/*.jsonl` (phase, pass and activity events), `~/.local/state/*/runs/*.jsonl` (other skill stores `history.py` reads), the usage records `scripts/usage/collect.py` reads, `/tmp/claude/unit/<run-id>/` session dirs (findings ledgers, prompts) where still present, `~/rust/hanadocs/issues` (proposals), git history of every worktree's `docs/plans/` and `docs/as-built/`
- **Key files:**
  - `commands/unit/direct.md` and `docs/delegate/*.md` — the phase contract: every step a unit director runs
  - `commands/plan/to_phased_plan.md`, `phase_review.md`, `shrink.md`, `to_as_built.md` — plan compile, fold-in, shrink, as-built
  - `commands/unit/checkpoint.md`, `brief.md`, `phase_report.md`, `ci.md` — phase helpers
  - `docs/delegate_plan_format.md` — the plan format; `docs/production_format.md` — <ProductionUnit/>
  - `commands/team_review.md`, `adversarial.md`, `api_review.md`, `module_review.md`, `astra_review.md`, `commands/producer/greenlight.md`, `to_production.md`, `commands/showrunner/proposal.md` — the doc pipeline before a plan
  - `scripts/history/history.py` — stage timings; its `PASS_STAGE`, `ACTIVITY_STAGE` and `ACTIVITY_ALIASES` map labels to stages
  - `scripts/direct/progress_history.py` — the recorder and its event model (`docs/as-built/plan-delegate-progress-history.md`)
  - `scripts/direct/findings.py` — the findings ledger
  - `scripts/usage/usage_report.py`, `collect.py` — tokens by session
  - `scripts/lib/user_zone.py` — user-facing times
  - `~/.claude/docs/decision_criteria.md` — what reaches the user
- **Test lanes:** `scripts/direct/` — `test_*.py` beside the script.
- **Test:** `python3 -m unittest discover -s scripts/direct -p 'test_phase_process.py'`, from the worktree root. Gate: `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only` from the worktree root (memory `shared-module-full-gate`).
- **Lint:** `basedpyright scripts/direct scripts/production` passes when its output has the `0 errors, 0 warnings` line; the exit status says nothing.
- **Invariants:**
  - Measuring reads every source read-only. It sends no message, push or alert, and starts no session (plan author: a measure must not add traffic).
  - Tests use fixtures; they never read the real `~/.local/state`, `/tmp/claude`, `~/rust/hanadocs` or transcripts.
  - Each number states its window and its count; medians and p90 come with n.
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`).
  - User-facing times use `scripts/lib/user_zone.py` (America/New_York, user 2026-10-12); log stamps stay UTC.
  - All text uses Simplified Technical English (user, 2026-10-18).
  - No step changes a live phase process file before the user approves it (user, 2026-10-20).

## Phases

### Phase 1 — One command measures each step of a phase and the doc pipeline before a plan, and the design doc maps the process  · status: done
**Depends on:** none

#### As-built

- `scripts/direct/phase_process.py baseline [--since DATE] [--until DATE] [--detail TABLE]` reads run records, usage.db and git. It writes no store of its own. The default window is the last 14 days.
- It prints five tables. Phase steps: wall time per step kind and activity, split equally where steps overlap; director time is the time no step covers; also the gap from phase end to the next phase start. Director tokens per phase, with the share before the first worker step and after the last. Contract load: each "Read … in full" file, followed one level out, with lines, bytes and cadence. Doc pipeline: first doc commit, first `run_started` and first `phase_started`, per doc kind and command. Plan churn: Work Order edits, decisions, and phases inserted after the run starts.
- Each table prints the top 10 rows and a summed `other` row, the window once in its title, and n on every number. `--detail TABLE` prints every row. A missing source prints `not recorded`. A source the Mac also keeps prints `natedev only`.
- Fix time is one stub row that names `history.py rounds` as its source.
- The design doc holds Map (22 phase-step rows and 7 doc-kind rows, each with its defining file or tag and its baseline row), Baseline (verbatim output for 2026-10-06 to 2026-10-20), Friction (measured and reported lines, each with a source) and Questions for the user (one).

**Files:**
- `scripts/direct/phase_process.py` — the measure command; read-only.
- `scripts/direct/test_phase_process.py` — 21 fixture tests.
- `docs/plans/sdlc-phases-design.md` — Map, Baseline, Friction, Questions.

**Binds later work:** Phase boundaries and fix time each sit behind one function in `phase_process.py`. The rounds.py swap replaces those two functions and the stub row, with fixture tests. One run record can hold more than one plan phase, so per-phase counts change after the swap. Candidate ranking reads the design doc's Map, Baseline, Friction and Questions sections.

**Gotchas:**
- Git history runs in the repository that holds each doc. A plan can name paths in other worktrees.
- Source docs come from bare paths and Markdown links in each header version, unioned.
- An open review start makes the after-last-step token share `not recorded` for that phase.

**Ruled out:** a second store of run data (the script reads the same sources as `history.py rounds`); taking the token-versus-dispatch-time question to the user alone (rank candidates both ways side by side, with one question at the end).

### Phase 2 — The design doc recommends changes to the phase process and the doc pipeline, split into steps  · status: done
**Depends on:** Phase 1

#### Work Order

**Goal:** the design doc proposes candidate changes for the largest friction, recommends the best, and splits them into small steps, each with the measure it moves.

**Spec:**
- First, when fix-rounds-unit's `scripts/history/rounds.py` is on the merge branch (the showrunner says so), merge the merge branch and swap `phase_process.py`'s phase-boundary and fix-time functions for rounds.py's phases and fix time; delete the stub row. Re-run `phase_process.py baseline` and replace the design doc's Baseline with the new output. If rounds.py is not merged yet, keep the stub and say so in Baseline; Phase 3 then does the swap first. The swap converts rounds.py's `0.0` open sentinel at the boundary into a named open-phase state, holds a completed phase interval and fix worker time in distinct types, and prints fix time labelled as worker time, apart from the wall-time shares. Fixture tests cover parallel repairs (worker time above wall time), an open phase, a mixed phase, a ledger-only round and a closure review.
- Doc pipeline: a doc whose first commit comes after its plan's first dispatch (a negative doc-to-phase time) is a separate state, `doc after dispatch`: counted apart with its n, listed under `--detail doc-pipeline`, and left out of the lead-time distributions, totals and cost ranking. One fixture test pins it; refresh the Baseline after it.
- Section **Candidates.** For each of the five largest Friction lines by time and the three largest by director tokens (time and tokens ranked apart, since their units differ; a line on both lists counts once), at least two candidate changes, each with what changes (files and steps), who it affects, and what it costs to build. Seeds, not limits:
  - **Shorter contract load:** split `direct.md` so a director reads only the part for the step it is on; each step names one file.
  - **Less director time between passes:** move Work Order compose for the next phase into the review wait of this one; merge checkpoint, shrink and phase review into one step.
  - **One design doc format:** one format and one command for a design doc before `/plan:to_phased_plan`, with the review it needs before compile.
  - **Fewer doc kinds:** a phased plan made straight from a proposal issue when the issue is small.
- Every candidate keeps the rules the user set: process changes are questions (Decisions); the three gods and the decision economy (`decision_criteria.md`); the scope lines beside guilds-unit, fix-rounds-unit and roles-unit. A candidate that changes a responsibility or a name names roles-unit as its owner and goes there.
- Section **Comparison.** One table: each candidate scored on each Phase 1 measure (expected direction and size, with the reason) and each rule (kept or broken). A candidate that breaks a user rule is out. Each candidate states its director-token cut and its dispatch-time cut, with judgment quality held. Below the table, two rankings side by side: by director tokens first and by dispatch time first (showrunner, 2026-10-20: which goal leads is the user's priority, answered best with each choice's concrete cost).
- Section **Recommendation.** The best set of candidates and why, in at most ten lines. Recommend director tokens first, and say what changes under the dispatch-time order.
- Section **Steps.** The change from today to the recommendation as numbered steps. Each step: what changes (files and steps), the measure it should move and by how much, its eligible observations (which phases or docs count, and the filter that selects them), how to undo it, and that it needs the user. Step 1 is the one with the largest expected effect per unit of work.
- Before the gate, the unit director runs `/adversarial` on the recommendation against the runner-up and `/astra_review` on the doc, and folds their findings into the doc.

**Files:**
- `docs/plans/sdlc-phases-design.md` — new sections above; Baseline replaced
- `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py` — the swap to rounds.py

**Workers:** `2 writers` — the swap and the doc are disjoint.
- `impl` — `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py`: the swap and the `doc after dispatch` state; then re-runs `phase_process.py baseline` for the doc's Comparison window and checks each number the doc cites against it
- `test` — opens as impl — `docs/plans/sdlc-phases-design.md`

**Constraints from prior phases:** the map, the baseline and the friction are in the design doc. The Phase 1 question (director tokens or dispatch time first) was not put to the user alone: the showrunner asked for both rankings in Comparison and one question at this phase's end. Phase boundaries and fix time in `phase_process.py` sit behind one function each, with fix time as a stub row, until rounds.py replaces them. rounds.py reaches sdlc in the merge that also carries fix-rounds-unit's work, and one run record can hold more than one plan phase, so the swap changes per-phase counts: the doc re-quotes every number it cites from the new baseline. rounds.py facts for the swap (fix-rounds-unit, 2026-10-20, from `load_phases`): `PhaseRounds.finished_at` is `0.0` for a phase with no `phase_finished`, which means open, never epoch 0; `PhaseRounds.fix_seconds` sums fix pass times, so it is worker time, and a round's wall time is `Round.started_at` to `Round.finished_at` (`0.0` while a pass in it is open); a ledger-only round has `fix_seconds` 0.0 and adds to the round count only; closure reviews are not in rounds.py, so the closure pass row stays a review pass; a phase with `check == "mixed"` holds more than one plan phase, so it is left out of per-phase step costs and medians and reported apart with its n; repair rounds whose `Round.cause == "merge"` count under fix, not under the merge row. A candidate that moves a duty checks roles-unit's Duty matrix (origin/sdlc-roles, `docs/plans/sdlc-roles-design.md`) and guilds-unit's change requests, which guilds-unit sends at its Phase 2 end.

**Acceptance gate:** the doc has the four sections; each candidate has a score on every measure and rule; each step names its measure, its size and how to undo it; the adversarial and astra findings are folded in. At phase end the unit director sends the showrunner one question for the user: the two rankings with each candidate's token cut and dispatch-time cut, the Recommendation (director tokens first) and Step 1, with the answers it can build and the one it recommends.

### Phase 3 — Build the first approved step  · status: todo
**Depends on:** Phase 2, and the user's approval of Step 1

#### Work Order

**Goal:** the step the user approved is live, and its measure is recorded before and after.

**Spec:**
- Before dispatch, the unit director copies the approved step from the design doc's Steps into this Spec verbatim (files, the change, the measure, how to undo it) and fills **Files** and **Workers** from it. A phase with no approved step does not dispatch: it waits on the showrunner's answer.
- If Phase 2 kept the fix-time stub, first do Phase 2's rounds.py swap (its Spec's first bullet, with its fixtures) and refresh the Baseline, so a change in phase counts is not read as a saving.
- If the step's eligible observations need a filter `phase_process.py` lacks (for example a unit or an after-deploy window), add it with a fixture test.
- Record the step's measure with `phase_process.py baseline` before the change, into the design doc under **Step log**: the measurement version (the `phase_process.py` commit), the eligible-observation filter, the population n and the before window.
- Edits to `commands/unit/direct.md` merge with any pending edits from guilds-unit or fix-rounds-unit that touch the same tags; the Work Order names each.

**Files:**
- `docs/plans/sdlc-phases-design.md` — Step log
- `commands/unit/direct.md` — the approved step's edits, with every other file the step names added before dispatch
- `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py` — the deferred rounds.py swap and any filter the step's measure needs

**Workers:** filled from the approved step.

**Constraints from prior phases:** the Steps section and the user's answer.

**Acceptance gate:** the gates in Delegation Context green; the checkpoint is verified and mergeable (the showrunner merges it and moves it to main); the Step log has the before measure, the measurement version, the filter, n and the window.

### Phase 4 — Measure the first step and propose the next  · status: todo
**Depends on:** Phase 3, the showrunner's word naming the hash and time the change reached main, and one full phase run by at least two units after that time

#### Work Order

**Goal:** the design doc says whether Step 1 moved its measure, and the next step goes to the user.

**Spec:**
- Run `phase_process.py baseline` at the Step log's measurement version and filter, for the window after the deploy time, and add the after numbers to the Step log with n for each.
- If the window holds no eligible observation (n 0, or below the n the step names), the result is `not yet measurable`: the Step log names the missing evidence and the condition for the next collection, and the phase waits on it; this is never read as a failed step.
- If the measure did not move by at least half the expected size, the Step log says so, names the likely reason, and proposes undoing it or changing it; that proposal is the question.
- Otherwise update Steps with what the measure taught, and pick the next step.
- At phase end, the unit director sends the showrunner the result and the next step as one question for the user. The next approved step becomes a new phase after this one, with the Work Order of Phase 3 and the step's own Spec.

**Files:**
- `docs/plans/sdlc-phases-design.md` — Step log, Steps

**Workers:** `1 writer + 1 tester` — one doc. `impl` runs the measure and writes the Step log.
- `impl` — `docs/plans/sdlc-phases-design.md`
- `test` — opens as impl — re-runs the measure and checks each number in the Step log; it writes no doc text

**Constraints from prior phases:** the Step log before measure (version, filter, n, window), the deploy hash and time from the showrunner, and the change Phase 3 shipped.

**Acceptance gate:** the Step log has before and after numbers with n and window; the question is sent.
