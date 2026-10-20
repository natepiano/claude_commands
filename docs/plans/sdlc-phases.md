# sdlc-phases

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready; long-running.** Own the phase process: measure how a `/unit:direct` phase runs from start to end and how design and documentation docs are made before a plan exists, find where time and tokens go, propose changes, then build and measure one approved change at a time.

> **Production: sdlc** — unit `phases-unit`; production doc `docs/plans/sdlc-production.md`

## Source

2026-10-20 16:47 EDT

I think we do need at least a unit director devoted in Nate Dev to managing the phase process.

how we prepare our documentation and design and how the design and documentation process runs

For active process redesigns, as we are currently undergoing with promotions and unit director renames and responsibility changes and process changes, those should come back to me as questions.

## What exists today (measured 2026-10-20 16:45–16:51 EDT by the plan author)

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

#### Work Order

**Goal:** `phase_process.py baseline` prints where a phase's time and tokens go, step by step, and how long a plan takes from first doc to first dispatch; the design doc holds the map, that baseline, the friction and the questions for the user.

**Spec:**
- `scripts/direct/phase_process.py baseline [--since DATE] [--until DATE]` (default window: the last 14 days). Each measure is one function that takes parsed records and returns a typed result, so tests feed fixtures. Output: one aligned table per group, each row with its window and n.
  1. **Phase steps.** For each finished phase in the window, split its wall time from `phase_started` to `phase_finished` into: each pass kind (impl, test, review, fix, as `history.py` maps them), each activity (labels normalized as `history.py` `_clean_label` does — import it, do not copy it), and **director time**: the time inside the phase that no pass or activity covers. Per step: n, median, p90, total, share of phase wall time. Also the time from `phase_finished` to the next `phase_started` in the same run (phase end through next Work Order). Fix time is one row; this script does not split it (fix-rounds-unit owns it).
  2. **Director tokens per phase.** Input and output tokens of the unit director session per phase, from the usage records where a session maps to a run; the share spent before the first pass of the phase (Work Order compose) and after the last (phase end). Where the records do not map, print `not recorded` and name the source that would.
  3. **Contract load.** For each file the phase contract tells a director to read in full on every phase (`direct.md` and each `docs/delegate/*.md` it names with "Read … in full"), its lines and bytes, and the sum per phase. Static: read from the repository.
  4. **Doc pipeline.** For each phased plan with a `run_started` event in the window: the first commit of the plan doc (and of any doc the plan header or Source names as its source) in git, the first `run_started`, the first `phase_started`, and the time between each; the commands that made the docs before it where git messages or the doc header name them. Print `not recorded` where no source has it.
  5. **Plan churn.** Per phased plan: Work Order edits between phases (commits that touch a `todo` phase), pending decisions opened and closed, phases inserted or split after the run started.
  - The script reads natedev's records. A table whose source the Mac also keeps says `natedev only`.
  - The script writes no store of its own. It reads the same sources fix-rounds-unit reads (`history.py`, the findings ledger, the phase records) and keeps no second copy of their facts (showrunner, 2026-10-20).
  - **Where the measures meet** (showrunner, 2026-10-20). fix-rounds-unit owns what a phase is in the run records, and the fix time, round count and round cause per phase: its `scripts/history/rounds.py` (`history.py rounds`). A run record can hold more than one plan phase. Until rounds.py merges, phase boundaries and fix time each sit behind one function that reads the `phase_started`/`phase_finished` events as they are, and fix time prints as one stub row that names `history.py rounds` as its source. This script does not split multi-phase records and does not count fix time a second time.
- The design doc `docs/plans/sdlc-phases-design.md`, sections:
  - **Map.** Each step of a phase, in order, from Work Order compose to the next phase's start: the tag or file that defines it, who runs it (director, worker, reviewer, script), what it reads, what it writes, and its row in the baseline. Then the same for the doc pipeline before a plan: each doc kind (proposal issue, review output, design doc, phased plan, production doc, unit plan, as-built), the command that writes it, the command that reads it, and its format file.
  - **Baseline.** The `phase_process.py baseline` output for the 14 days to the run, with the command and its window.
  - **Friction.** Each recorded problem with the phase process or the doc pipeline, one line each with its source: memories (for example `run-lib-suite-in-gates`, `clear-phase-review-wipes-all-prompts`, `no-worktree-writes-during-review`, `record-direct-review-fixes`, `correct-stale-as-builts-freely`), attempts logs, `-next.md` add-ons, and the largest baseline rows other than fix. Mark each `measured` or `reported`.
  - **Questions for the user.** Only what the map and friction need from the user: a goal the sources do not settle, or two rules that conflict. Each one plain line with its recommended answer.

**Files:**
- `scripts/direct/phase_process.py` — new
- `scripts/direct/test_phase_process.py` — new
- `docs/plans/sdlc-phases-design.md` — new

**Workers:** `2 writers` — the script and the design doc are disjoint.
- `impl` — `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py`
- `test` — opens as impl — the design doc's Map, Friction and Questions sections; it fills Baseline from `impl`'s output when the script runs

**Constraints from prior phases:** none.

**Acceptance gate:** `python3 -m unittest discover -s scripts/direct -p 'test_phase_process.py'` green; `basedpyright scripts/direct scripts/production` has the `0 errors, 0 warnings` line; the merge gate (Delegation Context → Test) green; `phase_process.py baseline` runs on real data on natedev in under 60 s and its output is in the doc; every Friction line names a source. At phase end the unit director sends the showrunner the Questions section as one message, never to the user: the showrunner brings each one to the user (showrunner, 2026-10-20). It also sends guilds-unit and fix-rounds-unit the Map.

### Phase 2 — The design doc recommends changes to the phase process and the doc pipeline, split into steps  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** the design doc proposes candidate changes for the largest friction, recommends the best, and splits them into small steps, each with the measure it moves.

**Spec:**
- First, when fix-rounds-unit's `scripts/history/rounds.py` is on the merge branch (the showrunner says so), merge the merge branch and swap `phase_process.py`'s phase-boundary and fix-time functions for rounds.py's phases and fix time; delete the stub row. Re-run `phase_process.py baseline` and replace the design doc's Baseline with the new output. If rounds.py is not merged yet, keep the stub and say so in Baseline.
- Section **Candidates.** For each of the five largest Friction lines by measured cost (time or tokens), at least two candidate changes, each with what changes (files and steps), who it affects, and what it costs to build. Seeds, not limits:
  - **Shorter contract load:** split `direct.md` so a director reads only the part for the step it is on; each step names one file.
  - **Less director time between passes:** move Work Order compose for the next phase into the review wait of this one; merge checkpoint, shrink and phase review into one step.
  - **One design doc format:** one format and one command for a design doc before `/plan:to_phased_plan`, with the review it needs before compile.
  - **Fewer doc kinds:** a phased plan made straight from a proposal issue when the issue is small.
- Every candidate keeps the rules the user set: process changes are questions (Decisions); the three gods and the decision economy (`decision_criteria.md`); the scope lines beside guilds-unit, fix-rounds-unit and roles-unit. A candidate that changes a responsibility or a name names roles-unit as its owner and goes there.
- Section **Comparison.** One table: each candidate scored on each Phase 1 measure (expected direction and size, with the reason) and each rule (kept or broken). A candidate that breaks a user rule is out.
- Section **Recommendation.** The best set of candidates and why, in at most ten lines.
- Section **Steps.** The change from today to the recommendation as numbered steps. Each step: what changes (files and steps), the measure it should move and by how much, how to undo it, and that it needs the user. Step 1 is the one with the largest expected effect per unit of work.
- Before the gate, the unit director runs `/adversarial` on the recommendation against the runner-up and `/astra_review` on the doc, and folds their findings into the doc.

**Files:**
- `docs/plans/sdlc-phases-design.md` — new sections above; Baseline replaced
- `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py` — the swap to rounds.py

**Workers:** `2 writers` — the swap and the doc are disjoint.
- `impl` — `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py`; then re-runs `phase_process.py baseline` for the doc's Comparison window and checks each number the doc cites against it
- `test` — opens as impl — `docs/plans/sdlc-phases-design.md`

**Constraints from prior phases:** the map, the baseline, the friction and the user's answers to the Phase 1 questions are in the design doc. Phase boundaries and fix time in `phase_process.py` sit behind one function each, with fix time as a stub row, until rounds.py replaces them.

**Acceptance gate:** the doc has the four sections; each candidate has a score on every measure and rule; each step names its measure, its size and how to undo it; the adversarial and astra findings are folded in. At phase end the unit director sends the showrunner the Recommendation and Step 1 as one question for the user, with the answers it can build and the one it recommends.

### Phase 3 — Build the first approved step  · status: todo
**Depends on:** Phase 2, and the user's approval of Step 1

#### Work Order

**Goal:** the step the user approved is live, and its measure is recorded before and after.

**Spec:**
- Before dispatch, the unit director copies the approved step from the design doc's Steps into this Spec verbatim (files, the change, the measure, how to undo it) and fills **Files** and **Workers** from it. A phase with no approved step does not dispatch: it waits on the showrunner's answer.
- Record the step's measure with `phase_process.py baseline` before the change, into the design doc under **Step log**.
- Edits to `commands/unit/direct.md` merge with any pending edits from guilds-unit or fix-rounds-unit that touch the same tags; the Work Order names each.

**Files:**
- `docs/plans/sdlc-phases-design.md` — Step log
- the files the approved step names

**Workers:** filled from the approved step.

**Constraints from prior phases:** the Steps section and the user's answer.

**Acceptance gate:** the gates in Delegation Context green; the change is live on main per the production's rule for `~/.claude`; the Step log has the before measure, the command and its window.

### Phase 4 — Measure the first step and propose the next  · status: todo
**Depends on:** Phase 3, and one full phase run by at least two units after the change went live

#### Work Order

**Goal:** the design doc says whether Step 1 moved its measure, and the next step goes to the user.

**Spec:**
- Run `phase_process.py baseline` for the window after the change, with the same filters as the before measure, and add the after numbers to the Step log with n for each.
- If the measure did not move by at least half the expected size, the Step log says so, names the likely reason, and proposes undoing it or changing it; that proposal is the question.
- Otherwise update Steps with what the measure taught, and pick the next step.
- At phase end, the unit director sends the showrunner the result and the next step as one question for the user. The next approved step becomes a new phase after this one, with the Work Order of Phase 3 and the step's own Spec.

**Files:**
- `docs/plans/sdlc-phases-design.md` — Step log, Steps

**Workers:** `1 writer + 1 tester` — one doc. `impl` runs the measure and writes the Step log.
- `impl` — `docs/plans/sdlc-phases-design.md`
- `test` — opens as impl — re-runs the measure and checks each number in the Step log; it writes no doc text

**Constraints from prior phases:** the Step log before measure and the change Phase 3 shipped.

**Acceptance gate:** the Step log has before and after numbers with n and window; the question is sent.
