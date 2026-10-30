# sdlc-phases

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready; long-running.** Own the phase process: measure how a `/unit:direct` phase runs from start to end and how design and documentation docs are made before a plan exists, find where time and tokens go, propose changes, then build and measure one approved change at a time.

> **Production: sdlc** — unit `phases-unit`; production doc `docs/plans/sdlc-production.md`

## Source

2026-10-20 16:47 AST

I think we do need at least a unit director devoted in Nate Dev to managing the phase process.

how we prepare our documentation and design and how the design and documentation process runs

For active process redesigns, as we are currently undergoing with promotions and unit director renames and responsibility changes and process changes, those should come back to me as questions.

## What exists today (measured 2026-10-20 16:45–16:51 AST by the plan author)

- **Phase process files:** `commands/unit/direct.md` (1579 lines), `docs/delegate/*.md` (8 files, 1185 lines), `commands/plan/*.md` (4 files, 1824 lines: `to_phased_plan`, `phase_review`, `shrink`, `to_as_built`), `commands/unit/*.md` helpers (`brief`, `checkpoint`, `ci`, `phase_report`, `eta`, `add_ons`, `report`, `style_review`), `docs/delegate_plan_format.md` (255 lines), `docs/production_format.md` (468 lines). Since then `docs/delegate/` moved to `docs/direct/` and `docs/delegate_plan_format.md` to `docs/direct_plan_format.md`.
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
  - `commands/unit/direct.md` and `docs/direct/*.md` — the phase contract: every step a unit director runs
  - `commands/plan/to_phased_plan.md`, `phase_review.md`, `shrink.md`, `to_as_built.md` — plan compile, fold-in, shrink, as-built
  - `commands/unit/checkpoint.md`, `brief.md`, `phase_report.md`, `ci.md` — phase helpers
  - `docs/direct_plan_format.md` — the plan format; `docs/production_format.md` — <ProductionUnit/>
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

#### As-built

- `phase_process.py` reads phases and fix time from `scripts/history/rounds.py`. An open phase is a named state, never epoch 0. A completed phase interval and fix worker time are distinct types. Fix time prints as worker time, apart from the wall-time shares. A mixed phase (one run record with more than one plan phase) is out of per-phase step costs and medians and is reported apart with its n.
- `doc after dispatch` is a separate doc-pipeline state: a doc whose first commit comes after its plan's first dispatch. It is counted apart with its n, listed under `--detail doc-pipeline`, and kept out of lead-time distributions, totals and cost ranking.
- `sdlc-phases-design.md` has Candidates, Comparison (one ranking by director tokens first, one by dispatch time first), Recommendation and Steps. Its Baseline is the `phase_process.py baseline` output after the rounds.py swap.
- The design doc records the user decision of 2026-10-28: director tokens come first. Step 1 is cheap A: `commands/unit/direct.md` becomes a short route, and each step reads only its own rule file. Cheap A targets a 3–7% token cut and a 0–3 s dispatch-time cut. Candidate C is not built now.

**Files:**
- `scripts/direct/phase_process.py` — the measure script: rounds.py phases and fix time, the `doc after dispatch` state
- `scripts/direct/test_phase_process.py` — fixtures for parallel repairs (worker time above wall time), open phase, mixed phase, ledger-only round, closure review, doc after dispatch
- `docs/plans/sdlc-phases-design.md` — map, baseline, friction, candidates, comparison, recommendation, steps

**Binds later work:** `read_usage` in `phase_process.py` sums input and all cache classes into one number and drops the model, so weighted tokens need a different read. `phase_boundaries` selects phases by finish time, and `measure_director_tokens` stops at phase end. Phase boundaries and fix time come from `scripts/history/rounds.py`: an open phase is a named state; fix time is worker time, apart from wall-time shares; a mixed phase is reported apart. `direct.md` costs about 41k tokens as a Read. `phase_process.py` has director tokens per phase, but no compactions per phase and no context size after reload.

**Gotchas:** closure reviews are not in rounds.py, so the closure pass row stays a review pass. A ledger-only round has zero fix time and adds to the round count only. A repair round with `cause == "merge"` counts under fix, not under the merge row. Inserted phases are about 2% of gaps.

**Ruled out:** a retrospective before sample for cheap A — a phase's start time does not prove which contract it read; the full A targets (10% tokens, 10 s) — the measured cut does not reach them.

### Phase 3 — The recorder records which phase contract each phase read, and the trial's three measures  · status: done
**Depends on:** Phase 2

#### As-built

- `progress_history.py start-phase` and `finish-phase` add a `contract_revision` field to their event: the `~/.claude` main HEAD commit, the sha256 of `commands/unit/direct.md` there, and the sha256 of each file its `<TagReferenceContract>` table names. A dirty file is hashed as it is on disk. A missing file gets a named missing state, never an empty hash. `direct.md` itself does not change. Readers accept an old event without the field.
- `phase_process.py baseline` gives each trial phase a `TrialExposure`: `BeforeChange`, `AfterChange` or `ExposureNotRecorded(reason)`. A phase is `BeforeChange` or `AfterChange` only when its start and finish records name the same `direct.md` hash and that hash is on one side: at or before `--before-at <commit>`, or on the route side of `--route-revision <commit>`. A hash on both sides or on no side, no record, or two different hashes give `ExposureNotRecorded`. No deploy time stands for exposure.
- `select_trial_phases` selects trial phases by start time and adds the following gap. A phase plus its gap is eligible only when the next phase in the same run starts with the same `direct.md` hash. `NoFollowingPhase` and `SharedDirector` (two phases open at once with one director) are counted apart with n, before the exposure filter. `phase_boundaries` keeps finish-time selection.
- Three measures per eligible phase, each with n. Weighted director tokens over the phase plus its gap is the primary measure: `read_usage` keeps token classes and the model apart, weights come from `scripts/usage/weights.py`, a model with no weight is a named unweighted state, and raw input and output print apart. Compactions per phase count `compact_boundary` entries. Context size after each reload is the raw input of the first request after the first contract Read that follows a boundary. `NoCompaction`, `NoReload` and `NotRecorded(reason)` are named states, never 0.
- Filters are `--units <a,b>`, `--exposure before|after` and `--detail trial`; windows are `[since, until)` in the user zone. Output starts with a 3-line summary. Repair medians use repaired phases only. Durations over 1000 s print in minutes.
- `tag_inventory.py` checks that each `<Tag/>` reference in `/unit:direct`'s resolved contract has exactly one body. Scope: `commands/unit/direct.md`, each file its `<TagReferenceContract>` table names, and the `docs/` Markdown links of those files, recursively. Fenced code, quoted examples, plan docs and other commands' own tags are out of scope. It fails on a missing body, two bodies, a stale path and a forwarding cycle.

**Files:**
- `scripts/direct/progress_history.py`, `scripts/direct/test_progress_history.py` — `contract_revision` on phase events
- `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py` — `TrialExposure`, eligibility, the three measures, the filters
- `scripts/direct/tag_inventory.py`, `scripts/direct/test_tag_inventory.py` — the route's structure check
- `commands/unit/checkpoint.md` — names `<RetainPhaseReservation/>`
- `docs/plans/sdlc-phases-design.md` — full A is 10-16 h; cheap A is Step 1; the before sample is prospective from the recording's deploy, and its freeze uses `--before-at`

**Binds later work:** the short route freezes the before sample with `phase_process.py baseline --exposure before --before-at <~/.claude main HEAD at the freeze> --units <units>`, moves rule bodies as they are, and must keep `tag_inventory.py` green. The 20-phase trial measures the after side with `--route-revision <route commit>`. Wait records and merge receipts sit beside `contract_revision` in the same run file, and `scripts/history` readers accept old and new events.

**Gotchas:** `test_phase_process.py` holds 3 tests that read the design doc's Recommendation, Comparison and Steps; an edit there must keep them green. A `**Depends on:**` line holds phase ids only, because `phase_table.py` reads every number on it as a phase id; other conditions go on a `**Waits for:**` line. `scripts/guilds/daily_read.py` needs finish-time selection from `phase_boundaries`.

**Ruled out:** a retrospective before sample — the design's Steps need a prospective one; start-time selection inside `phase_boundaries` — it breaks `daily_read.py`; deploy time as exposure — it does not prove which contract a phase read.

### Phase 4 — `direct.md` becomes a short route, and each step reads only its own rule file  · status: todo
**Depends on:** Phase 3, Phase 6, Phase 7, Phase 8, Phase 9, Phase 11, Phase 12, Phase 13

**Waits for:** Phases 3, 6, 7, 8, 9, 11, 12 and 13 on main, and postmaster-unit's write-once reply records on main (the showrunner names each hash and time); then at least twenty eligible `BeforeChange` phases (`--before-at` main HEAD) from at least two units after the latest of those times, so both samples run with the same recorder, wait records, launchers and `phase_process.py`. fix-rounds-unit's `<FixDispatch/>` edits to `commands/unit/direct.md` are on the sdlc merge branch since c79e5d12.

#### Work Order

**Goal:** the before sample is frozen in the Step log, then `commands/unit/direct.md` is a short route and each step's rules live in one file that the director reads at that step.

**Spec:**
- First, before any edit to a contract file, freeze the before sample. Fix both bounds: `--since` is the latest of the main times named in **Waits for**, and `--until` is the freeze time, written as UTC stamps in the Step log. Run one invocation per unit: `phase_process.py baseline --exposure before --before-at <~/.claude main HEAD at the freeze> --units <one unit> --since <the latest main time> --until <the freeze time>`. `render_trial` pools the primary measures across all selected units, so a run with several units gives no per-unit result. Run each unit again with `--detail trial` to list its eligible rows. Record in the design doc's **Step log** the `phase_process.py` commit, the before anchor, the filter, each unit, the window, n for each measure, each excluded state with n, median and p90 phase time per unit over the trial's eligible phases (the trial section's phase-time line), and the selected phase instances.
- Phase instances and matching metadata: the detail table prints only run and display phase id. For each detail row, read its phase start event in `~/.local/state/plan-delegate/runs/<run>.jsonl` (read-only) and record the run id, display phase id, `phase_instance_id`, start time, mode, scope class, worker model and gates. When one display id has more than one instance in the run, the start time inside the window selects the instance; when that does not select exactly one, the Step log names the row ambiguous. A field that the run file does not hold is `NotRecorded` for that row, never a blank. Phase 5 matches on these recorded values.
- Merge the sdlc merge branch. Build every move on its text (fix-rounds-unit's phase 5 and roles-unit's renames).
- The route: `direct.md` keeps the tags that can apply at any step after a compaction — state, `<TagReferenceContract>`, `<LongLivedWorkers>`, `<CoreContract>`, `<ProductionUnit>`, `<TurnEndGate>`, `<ToolingContract>`, `<DispatchContract>`, `<WorkerRuns>`, `<PauseForMove>`, `<CompactionContract>`, `<UserFacingText>`, `<ProgressContract>`, `<AuthorizationContract>`, `<DecisionRouting>`, `<Guild>`, `<GuildHeadStanding>`, `<HeldAstraRoles>` and the `**Standing:**` startup line — and one table row per moved tag: tag, file, the moment to read it. Moved tags go to files under `docs/direct/` beside `phase_end.md` and `run_phase_review.md`, grouped by step. Each step file starts with the list of contracts it depends on. A rule body moves byte-for-byte; only the route rows and each step file's header are new text.
- `tag_inventory.py` passes before and after the move, and lists the same tags.
- roles-unit's check passes on the tree: `python3 scripts/production/roles_audit.py check --root .` from the worktree root prints no issue, moved files included.
- Record `--route-revision` (the commit that makes the route) in the Step log.

**Files:**
- `commands/unit/direct.md` — the route
- `docs/direct/*.md` — new step files that take the moved tag bodies
- `docs/plans/sdlc-phases-design.md` — Step log

**Workers:** `1 writer + 1 tester` — one move.
- `impl` — `commands/unit/direct.md`, `docs/direct/*.md` (new files only), `docs/plans/sdlc-phases-design.md`: the before sample, then the move
- `test` — opens as test — runs `tag_inventory.py` and `roles_audit.py check`, and checks that each moved body is byte-identical to its source; writes no doc text

**Constraints from prior phases:** Deployed revisions: Phase 3 is merged as b5187294, and ~/.claude main has been bc9e8ad9 since 05:29 EDT 2026-10-29. Phase 6, Phase 7 and Phase 8 are pending: the showrunner names hash and time for each, and the Step log records them. Phase 8 changes which waits close, so a before sample that starts before Phase 8 is on main mixes two wait recorders. Phase 3's recorder records the `direct.md` hash at each phase start and finish, and `phase_process.py` reads exposure from it with `--before-at` (before the route exists) and `--route-revision`. Cheap A's guards (design doc, Recommendation): it does not rely on the 20k restore after compaction, the core rules are always read, each tag is found by its name, the tag inventory stays complete, and no `/unit:delegate` alias copies the text. Move each rule body as it is: repair rules stay fix-rounds-unit's text, `<Guild>` text stays guild-process-unit's (user, 2026-10-28). `direct.md` is a build-followups hub file: the checkpoint notice names each build-followups owner of a touched file, trial-merged against its tip. roles-unit renamed 9 "seat" words to "worker" in `direct.md`; keep them. Phase 9 added the run-cancel rule to `<WorkerRuns/>` and a worker sentence to `docs/direct/write_prompt_contract.md` item 3 (line 61). The complete `<WorkerRuns>` body stays in the always-read route in `direct.md`, and the worker sentence stays in `write_prompt_contract.md`, which this phase does not edit; the tester checks that both texts and their park and repair references are byte-identical before and after the move. Since Phase 13 the trial table prints short counts (`2.7M`); the Step log records each eligible phase's exact weighted tokens from the `--detail trial` rows (`str(row.tokens.weighted)` in `phase_process.py`), never the table cells.

**Acceptance gate:** the gates in Delegation Context green; the Step log holds the frozen before sample; `tag_inventory.py` passes before and after with the same tag list; `roles_audit.py check` prints no issue; each moved body is byte-identical to its source; the checkpoint is verified and mergeable.

### Phase 5 — Measure the first step and propose the next  · status: todo
**Depends on:** Phase 4

**Waits for:** the showrunner's word naming the hash and time the route reached main, then at least twenty eligible `AfterChange` phases from the same units as the before sample.

#### Work Order

**Goal:** the design doc says whether Step 1 moved its measure, and the next step goes to the user.

**Spec:**
- Fix both bounds before the run: `--since` is the time the route reached main, and `--until` is the collection time, written as UTC stamps in the Step log. Never use the script's moving fourteen-day default. Run one invocation per unit, because `render_trial` pools the primary measures across all selected units: `phase_process.py baseline --exposure after --route-revision <Phase 4's route commit> --units <one unit of the before sample> --since <the route main time> --until <the collection time>`, at the Step log's measurement version. Record the after phase instances and their matching metadata the same way Phase 4's freeze did (`--detail trial` per unit, then each row's phase start event). Add the after numbers to the Step log with n for each: weighted director tokens over the phase plus its following gap (the primary measure), compactions per phase and context size after each reload (reported, no target). Show each unit's results apart, and each excluded state (`ExposureNotRecorded`, `NoFollowingPhase`, `SharedDirector`, mixed) with n.
- Matching: the same units in both windows, with matched mode, scope class, worker model and gates. If they do not match, the result is `inconclusive`, with what did not match; it is never read as a pass or a failure.
- Counts: the n a step names counts usable primary observations (weighted tokens known). `Unweighted`, `NotRecorded`, `NoCompaction` and `NoReload` are reported with n; they are never 0 and never a failed saving.
- Quality held: median and p90 phase time per unit, over the trial's eligible phases (the trial section's phase-time line), do not rise against the Step log's before sample. A rise is reported with the step's result.
- If the window holds no eligible observation (n 0, or below the n the step names), the result is `not yet measurable`: the Step log names the missing evidence and the condition for the next collection, and the phase waits on it; this is never read as a failed step.
- Compute the saving from the exact per-phase weighted tokens the Step log holds (the `--detail trial` rows), never from the short-form table cells (`2.7M`), which hide differences near 1.5%.
- If the primary measure did not fall by at least 1.5% (half the low end of the 3–7% target), the Step log says so, names the likely reason, and proposes undoing it or changing it; that proposal is the question.
- Otherwise update Steps with what the measure taught, and pick the next step.
- At phase end, the unit director sends the showrunner the result and the next step as one question for the user. The next approved step becomes a new phase after this one. Write that step's own implementation Work Order: its Spec, its Files, its Workers, and its measurement requirements (the measure it must move, the target, the n it names and the excluded states it reports). Keep the frozen comparison protocol: fixed `--since` and `--until` bounds, one invocation per unit, recorded phase instances and matching metadata, and the before sample frozen in the Step log before any contract edit.

**Files:**
- `docs/plans/sdlc-phases-design.md` — Step log, Steps

**Workers:** `1 writer + 1 tester` — one doc. `impl` runs the measure and writes the Step log.
- `impl` — `docs/plans/sdlc-phases-design.md`
- `test` — opens as test — independently re-runs the frozen measure, recomputes the percentage from the exact observations (values near 1.5% included), and checks the Step log; it writes no doc text

**Constraints from prior phases:** Deployed revisions: Phase 3 is merged as b5187294, and ~/.claude main has been bc9e8ad9 since 05:29 EDT 2026-10-29. Phase 6, Phase 7, Phase 8 and Phase 4's route are pending: the showrunner names hash and time for each, and the Step log records them. The Step log before measure (version, filter, units, n, fixed window, phase instances with matching metadata), the deploy hash and time from the showrunner, and the change Phase 3 shipped. Step 1's target: weighted director tokens 3–7% lower per phase plus gap; gap 0–3 s. Exposure comes from the `direct.md` hash the recorder records at each phase start and finish (Phase 3), never from a deploy time; Phase 4 froze the before sample before the route. The next step is chosen from B, full A, G and I (design doc, Recommendation); C waits until dispatch time comes first. Phases 6, 7, 8, 9, 11, 12 and 13 and postmaster-unit's write-once reply records are on main before the before sample starts, so both samples use the same recorder, wait records, launchers and `phase_process.py`. A change to the recorder, a launcher or `phase_process.py` that reaches main between Phase 4's freeze and this collection makes the result `inconclusive`, naming the change, unless a new before sample is frozen after it. `test_phase_process.py` holds three tests that read the design doc's Recommendation, Comparison and Steps; every Step log or Recommendation edit keeps them green.

**Acceptance gate:** the Step log has before and after numbers with n and window, the matching result and the phase-time check; `python3 -m unittest discover -s scripts/direct -p 'test_phase_process.py'` passes, its design-doc tests included; the question is sent.

### Phase 6 — Each phase records its waits and their cause, and each merge leaves a receipt  · status: done
**Depends on:** Phase 3

#### As-built

- `progress_history.py start-wait --kind <kind> [--reason <text>]` prints a wait id, and `finish-wait --wait-id <id>` closes that one wait. Events (`wait_started`, `wait_finished`, `unclosed`) carry the phase instance id, the display phase id, unit, owner session and UTC times. Kinds: `WaitingForQuotaRefusal` (a provider refusal only, never a quota reading), `WaitingForMemoryOrSlot`, `WaitingForMergeGate`, `WaitingForUser`, `WaitingForSupplier`, `ReadyWithoutCapacity`. A probe session writes no wait. An open wait at `finish-phase` closes as `unclosed` with its start and no end.
- Emit points: `verify.sh` (memory admission, cargo token, build hold); `codex_mesh.py` (provider refusal, ready dispatch held for room); `merge_checkpoint.py` (merge gate); the Stop hook starts a user wait on a `decision:`/`gate:` turn and a supplier wait on a `blocked:` turn, and the UserPromptSubmit hook closes it through `turn_wait.json`.
- The `waits` verb returns waits for `[since, until)` with `covered_n` and `missing_n`. `start-phase` marks the instance as covered; a covered phase with no wait reads as covered zero, and a phase from a run with no wait history reads `HistoryNotRecorded`, never 0.
- Each pushed merge on both paths (checkpoint and child handoff) appends one receipt to `merge_receipts.jsonl` under the history root: project (from `Repository`), issue or plan phase, merge commit, source checkpoint, target branch, UTC time. A retry writes no second receipt; a rolled-back merge writes none. The `receipts` verb names a merge commit with no receipt `ReceiptMissing`.
- `phase_process.py baseline`'s trial section reports median and p90 phase time per unit, with n, over the same eligible trial phases as its three measures. `history.py` and `rounds.py` still read old run files.
- Wait attribution is incomplete: the slot comes from the shared app-server's start environment, several matching phase instances read as covered zero, and any user prompt closes a user or supplier wait. The phase "Each wait names the worker, checkpoint and sender that own it" fixes these.

**Files:**
- `scripts/direct/progress_history.py` — the recorder, the `waits` and `receipts` verbs; every emit point calls it
- `scripts/direct/phase_process.py` — trial phase time
- `scripts/direct/verify.sh`, `scripts/agents/codex_mesh.py`, `scripts/production/merge_checkpoint.py`, `scripts/hooks/stop-direct-continue.py`, `scripts/hooks/user-prompt-submit-conversation-pause.py` — emit points and receipts
- `scripts/hooks/test_direct_turn_wait_hooks.py`, `scripts/production/test_merge_checkpoint.py` — hook wait and receipt tests

**Binds later work:** the wait event shape, the six kinds, `HistoryNotRecorded` versus covered zero, and the receipt fields go to hierarchies-unit as a schema handoff; no script in this tree reads wait events yet. The baseline freeze records trial phase time from `phase_process.py baseline`.

**Gotchas:**
- `codex_mesh` runs one app-server per run, so a launcher's exported variables (`UNIT_PHASE_ID`, `UNIT_TEAM_ROLE`) are fixed at server start. Only `CODEX_THREAD_ID` is per thread.
- A unit finishes its phase before the showrunner merges it, so a merge-time record resolves a finished phase instance.
- A new import in a shared script breaks the fixture copy lists in `test_review_launcher.py` and `test_update_registration.py`.
- `phase_reservation.py` committed does not run in a repository that is not enrolled.

**Ruled out:** the slot taken from the shared app-server's start environment as a pass selector (it is fixed for every thread); choosing the newest phase instance for an ambiguous merge (it can name the wrong instance).

### Phase 7 — Each wait names the worker, checkpoint and sender that own it  · status: done
**Depends on:** Phase 6

#### As-built

- Worker waits: `verify.sh` gives `CODEX_THREAD_ID` to the recorder, and `codex_mesh.py`'s controller passes the worker name, run directory and thread id explicitly (it reads no environment identity). Both call one resolver, `resolve_worker_pass`, which maps the thread to exactly one `mesh_roster.json` worker and that worker to exactly one active pass. It returns `ResolvedWorkerPass` or `UnresolvedWaitAttribution` with its reason and candidates. Start and finish events carry `pass_instance_id` and `worker_name`. A director-run `verify.sh` wait is owned by the director (`--director` plus `CLAUDE_CODE_SESSION_ID`).
- Merge-gate waits: `<CheckpointCommit/>` runs `progress_history.py checkpoint --session-dir <S> --phase <id> --commit <hash>` after each commit, in every repository. `merge_checkpoint.py` turns the notice's hash into the full id (`git rev-parse --verify <commit>^{commit}`) and finds that event across the run files under the history root, this worktree's phases included. One match attributes the wait. Zero or several matches read as uncertain coverage for each candidate, never covered zero. A child handoff records a production-level handoff wait with no phase.
- User and supplier waits: the UserPromptSubmit hook ends `WaitingForUser` on typed input, or on a `From the user (via the showrunner):` relay from the expected showrunner address. It ends `WaitingForSupplier` only on a message from the supplier's current address. The production comes from the plan header through `resolve_production_unit`, which reads metadata only and accepts both header forms. Timers, task notices and other peers leave both open. The duplicate check compares the supplier too. Waits a phase leaves open close as `Unclosed` at phase completion, external waits included, under a shared lock.
- `waits` JSON reports `covered_n`, `attribution_miss_n`, `uncertain_phase_n`, `missing_n`, `production_handoff_n` and the handoffs, each miss with its reason and candidates.
- `memory_gate.sh` holds the memory-line body in one inner function; `buildlog_memory_line` and `verify.sh` call it.
- `phase_process.py baseline` prints durations of 2 h or more in hours.
- `workers.py` reads the production through `resolve_production_unit` and keeps `showrunners.canonical_slug` for the ledger path.

**Files:**
- `scripts/direct/progress_history.py` — resolver, attribution types, checkpoint verb, supplier identity, uncertain coverage, `waits` JSON
- `scripts/hooks/user-prompt-submit-conversation-pause.py` — sender-matched completion and the showrunner relay
- `scripts/direct/verify.sh`, `scripts/lint/memory_gate.sh` — thread attribution and the shared memory line
- `scripts/agents/codex_mesh.py` — explicit controller context
- `scripts/production/merge_checkpoint.py` — full-hash checkpoint lookup and the child handoff wait
- `commands/unit/checkpoint.md` — the checkpoint verb line
- `scripts/direct/workers.py`, `scripts/direct/phase_process.py`

**Binds later work:** the `waits` JSON schema went to hierarchies-unit at 820357a2. A reply that the postmaster forwards does not end a supplier wait yet; Phase 8 fixes it.

**Gotchas:**
- A launcher that never exits keeps its subscription to the worker thread and records the next compaction turn as work, so the worker reads busy and refuses follow-ups. End only the stale launcher pids.
- A merge in a `git clone --shared` scratch copy must fetch from the GitHub URL: the copy's `origin` is the worktree, so its `origin/sdlc` is the local ref.

**Ruled out:** the slot taken from a variable fixed at app-server start; the newest phase instance for an ambiguous checkpoint; checking a postmaster-forwarded reply against its log before the log exists (removed as containment).

### Phase 8 — A supplier reply that the postmaster forwards ends its wait  · status: done
**Depends on:** Phase 7; postmaster-unit's reply record (main abc8344f)

Phase 7's closure review found F007: the recorder checked a postmaster-forwarded reply against the postmaster's `replies/*.json` log, but `mods/postmaster/hooks/replies.ts` writes that log only after it forwards, so the recipient's hook ran before the evidence existed. Phase 7 removed that check as containment: a forwarded reply leaves the wait open. The showrunner sent the postmaster half to postmaster-unit (2026-10-29): it writes the reply record before it forwards, with a pending-forward test. This phase is the recorder half. It starts when the showrunner sends the postmaster main hash and the record fields; keep Phase 7's containment until then.

#### Work Order

**Goal:** a supplier reply forwarded by the postmaster ends the declared wait, and only when the postmaster's pre-forward record proves the original sender.

**Spec:**
- `progress_history.py` `_supplier_message` checks a postmaster-forwarded message against postmaster-unit's pre-forward reply record: the original socket equals the supplier's current address, the body and record time match, and this session is an intended target. Intended targets stay distinct from confirmed forwarded targets.
- A forwarded message with no unique matching record stays unverified, and the wait stays open.
- The record (showrunner, 2026-10-29; postmaster 82f47a15, then made write-once): `replies/<at_ms>-<n>.json`, written once before the first forward, with fields `at_ms`, `socket`, `name`, `session_id`, `senders`, `body`, `targets`. `targets` holds the intended session ids. `forwards/<at_ms>-<n>.json`, written once after the last forward, holds `{forwarded}`, the confirmed ids; it exists only when forwarding is done, and an empty `forwarded` means none confirmed. No file is rewritten, so a reply record read after the forward is complete. A failed first write sends no forward.
- Match: `socket` equals the supplier's current address, `body` equals the forwarded body, and this session is in `targets`. Merge origin/main first. Runs after Phase 7, because the files overlap.

**Tests:** verification runs while the forward is still pending and ends the wait; a missing, duplicate or mismatched record leaves it open; a direct reply still ends it.

**Files:**
- `scripts/direct/progress_history.py` — the forwarded-reply match
- `scripts/direct/test_progress_history.py` — the regression and rejection tests
- `scripts/hooks/test_direct_turn_wait_hooks.py` — the hook-level wait tests

Read-only references: `mods/postmaster/hooks/replies.ts`, `mods/postmaster/hooks/protocol.ts`, `mods/postmaster/hooks/outbox.ts` (record directory under `XDG_STATE_HOME`/`HOME`).

**Workers:** `1 writer + 1 tester`
- `fix3` — `scripts/direct/progress_history.py`: reply verification and the `finish_turn_wait` call boundary; sole hub owner.
- `merge` — opens as test — `scripts/direct/test_progress_history.py`, `scripts/hooks/test_direct_turn_wait_hooks.py`: record fixtures, the pending-forward regression, rejection and direct-delivery cases.

**Constraints from prior phases:** check that the outer wrapper `from` resolves to the postmaster's verified session or socket before reading any record; keep the direct-supplier path separate. Match the forward's `at_ms` to `record.at_ms`, the exact original body and the receiving `owner_session` in `targets`, to exactly one valid record; the record file counter is not the forward's suffix counter. A pending record proves origin and intended receiver, not confirmed forwarding. `finish_turn_wait` passes `owner_session` to `_supplier_message`. A verified reply alone never ends `WaitingForUser`; that still needs the expected showrunner's relay. Reuse `resolve_production_unit` and `_supplier_address`, keep the run lock and the exact wait-id and owner checks, and keep `VerifiedSupplierMessage | UnverifiedSupplierMessage`. Keep the legacy containment test's rejection and add new-schema pending and done cases. The record schema (abc8344f) is already in HEAD. Use `fix3` and `merge` until models-unit's reopen fix frees `impl` and `test`.

**Acceptance gate:** the gates in Delegation Context green; `basedpyright` on the changed files; the valid pending-forward regression fails on the Phase 7 tree and passes after; direct delivery and every rejection case stay green on both trees. Run `scripts.direct.test_progress_history` and `scripts.hooks.test_direct_turn_wait_hooks`. The full gate also needs unit-board's `rename.py:771` fix on sdlc; Phase 8 does not repair it.

### Phase 9 — A decision that makes a run useless cancels that run in the same turn  · status: done
**Depends on:** none

The showrunner sent this work on 2026-10-29 for the user, through hana (the tool-based-ui showrunner). At about 12:50 EDT hana parked cables-unit's phase 54b after its third repair round failed. The round's gate run (cargo nextest, started 11:39) kept running until about 13:03 only to finish a report, and it held memory that other runs waited for. No rule told anyone to cancel it.

#### Work Order

**Goal:** every director, showrunner and worker that starts runs reads one rule: when a decision makes a running or queued test, lint or build run useless, cancel that run in the same turn, and give back its memory and its place in the queue.

**Spec:**
- The rule names the cases: the phase is parked, the round failed, a newer tree replaces the run's tree, or a result already in hand answers the question. No run finishes only to complete a report.
- The rule names the cancel command for each kind of run that exists today, read from the code, not invented: a `mesh_runs.py` run (`kill -TERM -- -<pid>` on the pid in its record under `${SESSION_DIR}/mesh_runs/`), a detached `verify.sh` run, and a run waiting in the memory queue. Where no clean cancel exists today, the rule says so and points to the Phase 10 design; it does not invent a command.
- `/showrunner:produce`: a showrunner ruling that parks, stops or replaces work names the runs to cancel.
- `/unit:direct`: the rule sits in `<WorkerRuns/>`, and the repair-round and park paths point to it.
- Worker prompts that start runs: `<WritePromptContract/>` item 3's verbatim `## Builds and tests` section carries a one-sentence form of the rule for workers (cancel a run of your own that a later edit or result makes useless).
- Simplified Technical English. No other text moves; Phase 4 later moves `direct.md` bodies byte-for-byte and keeps this text.

**Files:**
- `commands/unit/direct.md` — `<WorkerRuns/>` rule and its pointers
- `commands/showrunner/produce.md` — the ruling rule
- `docs/direct/write_prompt_contract.md` — the worker sentence in item 3

**Workers:** `1 writer` — three short doc edits; no test lane.
- `rule` — opens as impl — all three files; it reads `scripts/agents/mesh_runs.py`, `scripts/direct/verify.sh` and the memory queue code read-only to name each cancel command.

**Constraints from prior phases:** `direct.md` is a build-followups hub file: the checkpoint notice names each build-followups owner of a touched file, trial-merged against its tip. Keep roles-unit's "worker" wording. verify-unit (builds production) owns `verify.sh` and the memory queue; this phase changes no code there.

**Acceptance gate:** `tag_inventory.py` passes with the same tag list; `python3 scripts/production/roles_audit.py check --root .` prints no issue; each cancel command the rule names exists in the code it cites; the checkpoint is verified and mergeable.

### Phase 10 — Findings and a design: checkable cancel criteria and one stop command  · status: done
**Depends on:** none

The same showrunner request (2026-10-29), Ask 2: research and a design, no build. The showrunner passes the design to hana.

#### Work Order

**Goal:** a design doc says which cancel criteria a tool can check without judgment, with evidence for each, and designs one command that marks a phase or round stopped and cancels every run tied to it.

**Spec:**
- (a) Criteria. Test each against the live code and records, and state for each: the source a tool reads, whether it is reliable today, and the gap if not.
  - The run's phase or reservation is released, parked or marked failed.
  - Its tree or clone is no longer the tip of any live branch or phase.
  - Its unit recorded a stop or park.
  - The session that owns it is gone.
  - A newer run for the same phase and step is queued.
- (b) One command for the moment work is found faulty (a failed round, a park, a stop rule). It marks the phase or round stopped and cancels every run tied to it: `verify.sh` runs, `mesh_runs` runs and the memory queue. Exit 77 means cancelled, not failed. The design names the run-to-phase link each kind of run carries today, the link it must gain, the record the stop writes, and who calls the command (director, showrunner, repair round).
- builds is planning verify-made build copies with provenance paths; a copy path that names its phase serves criterion (a). The design states what path shape it needs from builds.
- verify-unit (builds production) owns `verify.sh` and the queue. The design marks every change there as builds' to agree; the unit director sends builds that part before the design goes to the showrunner.
- No code changes.

**Files:**
- `docs/plans/sdlc-run-cancel-design.md` — findings and design (new)

**Workers:** `1 designer` — an Astra designer under `<HeldAstraRoles/>`, with one reviewer.
- `designer` — `docs/plans/sdlc-run-cancel-design.md`; reads the code read-only.

**Constraints from prior phases:** Phase 6 and Phase 7 wait records name the worker pass, checkpoint and sender that own each wait (`progress_history.py`); the stop record should reuse those identities. `mesh_runs.py` records live in `${SESSION_DIR}/mesh_runs/`, each log beside its record. Reservation states are in `phase_reservation.py`.

**Acceptance gate:** each criterion has its source, reliability and gap with a file reference; the command design names its links, records, exit codes and callers; builds' part is marked and sent; the reviewer's findings are answered; the design is sent to the showrunner.

### Phase 10b — The run-stop design carries its owners' answers  · status: done
**Depends on:** Phase 10

Phase 10 sent builds' part of the design. The builds showrunner answered at 13:58 EDT, and verify-unit and resources-unit answer their items by 15:30 EDT.

#### Work Order

**Goal:** `docs/plans/sdlc-run-cancel-design.md` carries each owner's answer, and the design goes to the sdlc showrunner.

**Spec:**
- Copy paths follow the user's rule of 2026-10-29 (`/tmp/claude/copies/<showrunner>-<unit>/<phase>-<purpose>`, the folder name from `roster.UnitLabels.folder_name()` on labels from `roster.unit_labels_for`, owned by verify-unit's `docs/plans/verify-copies.md`). The design points to that rule and defines no second path; the run-stop provenance goes in a file inside that folder.
- `board.sh` is natedev's file. The TERM and release order builds on verify-unit's token fix (no reclaim from a live holder, token renewed with `--pid`).
- Fold in verify-unit's and resources-unit's answers on items 1-4, 6 and the grace numbers. An answer that changes a decision is stated as theirs; an item still open stays under Open questions.
- No code changes.

**Files:**
- `docs/plans/sdlc-run-cancel-design.md`

**Workers:** none — the unit director makes these doc edits.

**Constraints from prior phases:** Phase 10's design and its round 1 review answers (F001-F008) stay as accepted.

**Acceptance gate:** each owner's answer is in the doc or named as open; no second copy path rule is in the doc; the design is sent to the showrunner.

### Phase 11 — Each quota, memory and slot wait names who holds what it waits on  · status: done
**Depends on:** Phase 8

The sdlc showrunner's add-on for hierarchies-unit (2026-10-29). Wait records (Phase 6 and 7) carry `owner_session`, `wait_attribution` and `expected_supplier`, but not who holds the account, slot or memory being waited on. hierarchies counts a Hana wait as caused from outside only when a record names that holder; its four-week experiment (its Phase 4, HI7) waits on this field. It ships its typed reader now and infers no outside cause until the field exists.

#### Work Order

**Goal:** the recorder takes and reports, for each `WaitingForQuotaRefusal`, `ReadyWithoutCapacity` and `WaitingForMemoryOrSlot` wait, the named holder of the resource waited on, and the producers that own the data get an exact hand-off for passing it.

**Spec:**
- Recorder (`scripts/direct/progress_history.py`):
  - A tagged holder value: `NamedHolder(resource_kind, resource, holder)` with `resource_kind` one of `account`, `slot`, `memory`, `resource` the account label, slot name or memory pool, and `holder` tagged `SessionHolder(session_id)` or `UnitHolder(unit)`; or `HolderNotNamed(reason)`. No empty strings or nulls carry state.
  - `start-wait` takes `--holder-resource-kind`, `--holder-resource` and one of `--holder-session` or `--holder-unit`, all or none. None records `HolderNotNamed("producer named no holder")`. A partial set is refused with exit 2.
  - A holder can change during one wait (the queue moves). A new verb `wait-holder --session-dir D --wait-id ID <holder flags>` appends a `wait_holder_seen` event for an open wait; it refuses a closed or unknown wait id.
  - `waits` output gives each wait `holders`: every holder seen, in order, each with its time, or one `HolderNotNamed`. Old records with no holder events read as `HolderNotNamed("recorded before holders")`.
  - Holders apply only to the three wait kinds above; a holder flag on another kind is refused.
- Two `waits` defects the showrunner folded in (2026-10-29, found by hierarchies' Phase 4 review; both give wrong results):
  - A wait event row with no `wait_id` is dropped without a count: `_waits_by_id()` skips it and `ignored_n` does not count it, so a reader sees zero covered waits for that phase. Count each such row in `ignored_n` and in a new top-level `wait_without_id_n`; the phase row it names (same `run_id` and `phase_instance_id`) reads `HistoryNotRecorded` with reason `wait record has no wait id` and `wait_without_id_n`, never `Covered`.
  - The phase rows keep only the worktree name and drop the plan, production and repository, so a retired phase (worktree `widget-enhancements`, plan `tool-based-ui-widget.md`) cannot be traced to its production and its `HistoryNotRecorded` row drops out of the Hana counts. Each phase row carries `plan_doc`, `production` and `repository`. `phase_started` records `production`, `unit` and `repository` from now on; an older event resolves them once per run from disk (the plan header's `> **Production:**` line and the worktree's git common directory). A value that cannot be found is a typed `{"state": "NotRecorded", "reason": ...}`, never an empty string.
- Producers are other units' files. Write a hand-off for each, with the exact flags, and send it through the sdlc showrunner:
  - builds (verify-unit, resources-unit): `verify.sh`'s `phase_wait_start` passes the board token owner for a slot wait, the memory reservation owner for a memory wait, and the build hold's holder for a hold wait, and calls `wait-holder` when the holder changes. Agree it with builds [a703f3].
  - natedev models-unit: `codex_mesh.py`'s `_mesh_phase_wait` passes the account label it waits on for quota and capacity waits.
- Tell hierarchies-unit the field shape once the checkpoint lands.

**Files:**
- `scripts/direct/progress_history.py`
- `scripts/direct/test_progress_history.py`

**Workers:** `1 impl` — writes the recorder change and its tests.
- `impl` — both files above.

**Constraints from prior phases:** Phase 6 and 7 wait identities (`owner_session`, `wait_attribution`, `expected_supplier`) stay as they are. Phase 8's typed record pattern (frozen dataclass from fields, `ValueError` on bad fields) is the model for the holder type. Type check: `basedpyright scripts/direct/progress_history.py scripts/direct/test_progress_history.py` reads 0 errors, 0 warnings, 0 notes. Tests: `python3 -m unittest scripts.direct.test_progress_history`.

**Acceptance gate:** a test opens each of the three wait kinds with a named session holder and a named unit holder, changes the holder once with `wait-holder`, and reads both holders back in order from `waits`; a wait opened with no holder flags reads `HolderNotNamed`; a partial flag set, a holder on `WaitingForUser`, and `wait-holder` on a closed wait each exit 2; an old history file with no holder events reads `HolderNotNamed("recorded before holders")`. A wait row with no `wait_id` raises `ignored_n` and `wait_without_id_n` and makes its phase `HistoryNotRecorded`; a new phase row and an old one whose worktree and plan are on disk both carry `plan_doc`, `production` and `repository`, and an old one whose worktree is gone carries the typed not-recorded value. The two hand-offs are sent.

### Phase 12 — The postmaster reply reader reads write-once records  · status: done
**Depends on:** Phase 8

postmaster-unit takes Phase 8's atomic-write note (showrunner, 2026-10-29; natedev-postmaster dfb1ef64). No reply file is rewritten: `replies/<at_ms>-<n>.json` is written once before the first forward with `{at_ms, socket, name, session_id, senders, body, targets}`, and a new `forwards/<at_ms>-<n>.json` with `{forwarded}` is written once after the last forward. When that change is on main, Phase 8's reader refuses every new record (it requires `forward`), so forwarded replies no longer end waits. This phase checkpoints ahead of Phase 11 and the showrunner merges it at once.

#### Work Order

**Goal:** a forwarded supplier reply whose record has the write-once form ends the wait, exactly as Phase 8's checks decide.

**Spec:**
- `PostmasterReplyRecord` drops `forward` and `forwarded`; `from_fields` no longer reads them. A record that still carries them (written before the change) reads the same, its extra fields unread.
- `_read_postmaster_reply_record` drops the re-reads: one read; unreadable or invalid JSON is a rejected record.
- The reader does not read `forwards/`: `targets` decides intent, and no check uses the confirmed ids.
- The match rules, rejections and wait ending of Phase 8 stay as they are.

**Files:**
- `scripts/direct/progress_history.py`
- `scripts/direct/test_progress_history.py`

**Workers:** `1 impl` — writes the reader change and its tests in a review copy, because Phase 11's edits to the same file are uncommitted in the worktree.

**Constraints from prior phases:** Phase 8's typed record pattern and rejections stay. Type check: `basedpyright scripts/direct/progress_history.py scripts/direct/test_progress_history.py` reads 0 errors, 0 warnings, 0 notes. Tests: `python3 -m unittest scripts.direct.test_progress_history`.

Assertion-Change: `test_forwarded_reply_is_verified_when_a_torn_record_read_recovers` is removed, because the re-read it pins is removed.
Assertion-Change: `test_persistently_malformed_reply_record_is_rejected_after_bounded_retries` expects one read and no sleep, not five reads and four sleeps; the rejection and the open wait stay.
Assertion-Change: `test_invalid_reply_record_schema_leaves_the_supplier_wait_open` drops its `forward="unknown"` and `forwarded="director"` cases, because those fields are no longer read; a record that carries them is accepted.

**Acceptance gate:** a forwarded reply whose record has the write-once fields ends the wait, with and without a `forwards/` file; an old-form record still ends it; a missing, mismatched or duplicate record still leaves it open. Tests pass on a trial merge with natedev-postmaster dfb1ef64 (not merged into this branch), and postmaster's own tests pass there too.

### Phase 13 — The trial measures table fits a phone  · status: done
**Depends on:** Phase 11

The showrunner returned change-review row C454 (2026-10-29): `phase_process.py baseline --exposure before --before-at HEAD --detail trial` prints its trial measures table 95-106 characters wide, which wraps on a phone, and the row stated a fixed n that has since moved.

#### Work Order

**Goal:** the trial measures table reads on a phone without wrapping.

**Spec:**
- `scripts/direct/phase_process.py` trial table: values in short form (`2.7M`, `20.1M`, `61K`; whole numbers below 1,000 as they are), no `not recorded` column (a not-recorded count above 0 prints as one line under the table), and a header and title no wider than the rows. Every line is at most 60 characters.
- The checkpoint's Change-Review line states no fixed n.

**Files:**
- `scripts/direct/phase_process.py`
- `scripts/direct/test_phase_process.py`

**Workers:** `1 impl`.

**Acceptance gate:** a test renders the table from fixture phases with values in the millions and a not-recorded count, and checks every line is at most 60 characters, the short forms, and the not-recorded line; the live command's lines 6 to 11 are at most 60 characters.

### Phase 15 — A rerun review lens checks every change that writes lasting state  · status: done
**Depends on:** none

The user approved this work through the showrunner (2026-10-29, ranked before Phase 14: "we have been bit hard by this too many times"). Today shutdown kept old "stopped" records for 10 live sessions; each rerun of down/now trusted the record and did nothing; a failed restart moved the records to history/, so a rerun had nothing to restore; `set -e` stopped `herdr-up.sh` at the first failed account. Earlier faults had the same shape: `launch_pending` stayed true after a failed start; a "restarting" claim blocked a rerun until it timed out; a restart-job wait never cleared; a restore skipped a session whose record held an old path. No review lens asks these questions today.

#### Work Order

**Goal:** a phase that writes lasting state gets a third reviewer, the `rerun` lens, at the same time as `adversary` and `contract`; a phase that writes none does not.

**Spec:**
- `docs/direct/dual_review.md` `<TeamReview/>`: add the `rerun` lens row and its questions to the lens table and the question list. The lens reads, for each record, mark, lock, claim, state file or moved file the change writes:
  1. If the command stops at any line and runs again, does it finish?
  2. Does it trust a stored status without checking the live thing (pid and process start time, path, lock holder)?
  3. Does a failure move or delete what a rerun needs?
  4. Does one failure stop other, unrelated steps (`set -e` over a list)?
  5. Does each mark have an expiry or an owner check, and a command that clears it?
  Under the questions, one real case each, for the reviewer:
  1. Restart of claude3 never ran after one account failed.
  2. `down` trusted a "stopped" record for a session that was live.
  3. A failed restore moved the records to `history/`, so the rerun had nothing to restore.
  4. `set -e` in `herdr-up.sh` stopped every later account at the first failed one.
  5. `launch_pending` and the "restarting" claim had no command to clear them.
  A failed answer is a defect, fixed in the phase (severity blocker or minor, as for any finding). The prompt closes with the rerun case the reviewer built, or a statement that it could not build one.
- When the lens runs: it is armed when the phase writes lasting state, that is, a record, mark, lock, claim, state file, a file move or delete, or a list of steps where one step can fail. Two triggers, either one arms it:
  - the Work Order's `**State:**` line names what the phase writes (`none` and an absent line do not arm it);
  - `scripts/direct/rerun_lens.py arm --diff <file>` finds such a write in the phase diff, so a missing or wrong `State:` line never skips the lens. It prints `armed` with each hit as `path:line — <kind>`, or `not armed`. Kinds: file write or append outside a test fixture; `os.replace`/`rename`/`shutil.move`/`unlink`/`rmtree`; `fcntl.flock` or a lock or claim file; a JSON/JSONL state write; in shell, `mv`, `rm`, `> file`, `mkdir` used as a lock, and `set -e` in a script that loops over a list; in Rust, `std::fs::write`, `File::create`, `OpenOptions` with append or write, `fs::rename`, `remove_file`, `remove_dir_all`, and the lock crates `fs2` and `fd-lock` (Rust tools here write lasting state too, such as cargo-berth reservations and claims). Hits in test files, docs and fixtures do not count. It reads only; it changes nothing.
  - Pure computation and docs-only changes skip it. The diff check decides in every repo, Rust repos included. `<DualReview/>` runs the check before it launches the broad review; when armed, the `rerun` reviewer launches with the other two, on the same review copy, and its findings file is `review_findings_<pass>_rerun.txt`.
- The `rerun` lens is a valid lens everywhere a lens is checked: `review.sh` (closed set and slot: `rerun` takes the `review` slot, as `ux` does; allowed on `--pool code`), `assign.py --lens` choices, `findings.py` `REVIEW_LENSES` and its `open --lens`, `usage/agents_log.py` `Lens`/`LENSES`, and every other reader of the lens set (`review_pool_report.py`, `progress_history.py`, `history/rounds.py`). Where the set is written twice, it is written once and imported, so a new lens is one edit.
- `docs/direct/compose_work_order.md` and `docs/direct_plan_format.md`: add the optional `**State:**` field after `**Workers:**`: what lasting state the phase writes, or `none`. `berth.work_order validate` accepts a Work Order with or without it.
- `commands/unit/direct.md`: the `findings.py open` row lists `rerun` among the lenses. No other route text changes.

**Files:**
- `docs/direct/dual_review.md`
- `docs/direct/compose_work_order.md`
- `docs/direct_plan_format.md`
- `commands/unit/direct.md` — one table row
- `scripts/direct/rerun_lens.py` — new
- `scripts/direct/test_rerun_lens.py` — new
- `scripts/direct/review.sh`
- `scripts/direct/assign.py`
- `scripts/direct/findings.py`
- `scripts/usage/agents_log.py`
- `scripts/direct/review_pool_report.py`
- `scripts/direct/progress_history.py`
- `scripts/history/rounds.py`
- `scripts/direct/test_review_launcher.py`
- `scripts/direct/test_review_launcher_pool.py`
- `scripts/direct/test_assign_launcher.py`
- `scripts/direct/test_findings.py`
- `scripts/usage/test_agents_log.py`
- `scripts/direct/test_review_pool_report.py`
- `scripts/direct/test_progress_history.py`
- `scripts/history/test_rounds.py`

**Workers:** `2 writers`.
- `fix3` (impl) — `scripts/**`: the lens set, `rerun_lens.py` and the tests
- `design` (impl) — the four docs

**State:** none — docs, review launch scripts and a read-only diff check.

**Acceptance gate:** `rerun_lens.py arm` gives `armed` on a fixture diff that writes a state file, moves a record, takes a lock, and adds `set -e` over an account loop, each hit named with path:line; it gives `armed` on a Rust fixture diff that writes a reservation file with `std::fs::write`, renames it with `fs::rename` and takes an `fd-lock`; it gives `not armed` on a docs-only diff, a pure-computation diff and a diff whose only writes are in tests. On today's shutdown fix diffs (when present on main) it gives `armed`. `review.sh --lens rerun` passes its launch check; `findings.py open --lens rerun` records the lens; `assign.py --lens rerun` is accepted. The `review.sh`, `assign`, `findings`, `agents_log`, `review_pool_report`, `progress_history` and `rounds` suites stay green; basedpyright 0/0/0. `berth.work_order validate` passes this plan's todo phases.

### Phase 16 — One rerun sweep of the scripts names each fault and its owner  · status: done
**Depends on:** Phase 15

#### Work Order

**Goal:** the showrunner has one list of rerun faults in `~/.claude/scripts`, each with path:line, the question it fails and the owner unit.

**Spec:**
- Run the `rerun` lens's five questions over the scripts in `~/.claude/scripts` at the sdlc merge-branch tip, in this order: shutdown, herdr, claims, holds, leases and review-pool scripts first, then the rest that `rerun_lens.py arm` marks armed when run on the whole file.
- Each fault is one row: `path:line`, question number, what happens on a rerun (the stop line and the result), and the owner unit (from the file's last checkpoint subject, or the production doc's owner map). A fault already fixed on the tip is not a row.
- The unit director checks each row against the file before it is sent; a row that does not hold is dropped.
- Send the list to the showrunner as one message. The sweep fixes nothing; the showrunner routes each row to its owner.
- Holder evidence stays visible when it is malformed (showrunner, 2026-10-29, from the hierarchies phase 10 review; a wrong result, due at this checkpoint). `progress_history.py waits` reads each wait's holder record. Today a present but malformed `holder` (such as `[]`) becomes `HolderNotNamed("recorded before holders")`, and unknown holder fields are dropped before any reader sees them, so a malformed record reaches the measure as valid evidence. A new named state, `MalformedHolder`, carries the raw value and the reason. Only an absent `holder` key means recorded before holders. A record with a field outside its state's schema is malformed. Every reader of the `holders` output that counts holder evidence treats `MalformedHolder` as not valid evidence and shows it.
- A phase's `production` in `progress_history.py waits` is a production name, never a path (showrunner, 2026-10-29, from the C473 vet). A plan header such as `> **Production:** docs/hana/tool-based-ui-production.md — unit …` names the production doc; `_phase_plan_names` reports that path as the production today. When the header value is a path to a production doc, read the name from that doc's `# Production — <name>` heading; a stored `phase_started` value that is a path reads the same way. A doc that cannot be read or has no heading gives `PhaseMetadataNotRecorded` with the reason, never the path.
- One heading reader, with no import cycle (merge gate, 2026-10-29: basedpyright `reportImportCycles` on `progress_history.py` -> `production_lifecycle.py` -> `merge_checkpoint.py`). `scripts/production/production_doc.py`, which imports only the standard library, gains `production_name(lines)` returning `ProductionName(value)` or `ProductionNameNotRecorded(reason)`. `production_lifecycle.production_name` keeps its callers and its `LifecycleStop`, and reads through it. `progress_history.py` imports `production_doc` at module level, with no `sys.path` change and no import inside a function.

**Files:**
- `docs/plans/sdlc-rerun-sweep.md` — new: the list, with the tip hash and the time
- `scripts/direct/progress_history.py` — the `MalformedHolder` state and the production name from a header path
- `scripts/direct/test_progress_history.py` — tests for a malformed holder and unknown fields
- `scripts/production/production_doc.py` — the shared production name reader
- `scripts/production/production_lifecycle.py` — reads the name through `production_doc`
- `scripts/production/test_production_lifecycle.py` — tests for the shared reader
- `scripts/direct/review_launcher_test_support.py` — its copied scripts tree gains `production_doc.py`
- `scripts/direct/implement_launcher_test_support.py` — its copied scripts tree gains `production_doc.py`

**Workers:** `2 writers` — `fix3` reads shutdown, herdr and claims scripts; `design` reads holds, leases, review-pool and the rest. Each writes only its own section of the report. `fix2` writes the holder fix.

**State:** none — the sweep reads only and writes one report; the holder fix changes how stored holder records are read, not what is written.

**Acceptance gate:** every row names path:line, a question 1-5, the rerun result and an owner; the unit director has checked each row against the tip; the list is sent. A wait whose holder is `[]`, or a named holder with an unknown field, comes back from `waits` (its output is JSON) as `MalformedHolder` with the raw value, and an absent holder key still reads as recorded before holders; a phase whose plan header names a production doc path reports that doc's production name; basedpyright over the whole `scripts/` tree reports no import cycle; the `progress_history` suite stays green; basedpyright 0/0/0.

### Phase 14 — A plan's Depends line counts only this plan's own phases  · status: done
**Depends on:** Phase 15 (order only: the showrunner ranks the rerun lens first)

The showrunner sent a wrong result (2026-10-29): hierarchies-unit's phase 9 notice printed "Phase 6 needs Phase 2026 (not in the plan)". `phase_table.py parallel` reads each phase's `**Depends on:**` list from `plan_history.py`, which takes every number in the line (`re.findall(r"\b\d+[a-z]?\b", ...)`, near line 117). So a date (`2026-11-12`), a time (`19:00`), a commit hash with leading digits, another unit's phase (`phases-unit Phase 11`), and other numbered things (`Steps 1 and 4`) all count as this plan's phases.

#### Work Order

**Goal:** a phase's dependency list holds only the phases of its own plan that its `**Depends on:**` line names.

**Spec:**
- `scripts/direct/plan_history.py` reads phase ids only from `Phase <id>` and `Phases <id>, <id> and <id>` spans (either case). A span that another unit names (`phases-unit Phase 11`, `models-unit's phase 4`) does not count. Numbers outside a span (dates, times, hashes, `Steps 1 and 4`, `(numbering only)`) never count. `none` gives an empty list; an absent line stays `None`.
- A named id that is not a phase of the plan still prints as `not in the plan`: that message is for a real wrong reference, and it stays.
- Fix it in the parser, where the defect is; `phase_table.py` keeps reading `depends_on` as it does.

**Files:**
- `scripts/direct/plan_history.py`
- `scripts/direct/test_progress_history.py` — the plan-parse tests
- `scripts/direct/test_plan_history.py` — the dependency-parser tests

**Workers:** `1 impl`.

**Acceptance gate:** a test parses these Depends lines and gets exactly the ids shown: `the checkpoints of Phases 3, 4 and 5, and the three review records: the Phase 3 and Phase 5 re-runs, which the unit director runs on 2026-11-12 (two weeks after both merged into sdlc as 3c5dea67 on 2026-10-29) and records in the design doc under Steps 1 and 4` → 3, 4, 5; `phases-unit Phase 11 (the wait-holder field and the phase origin fields) merged into sdlc; sdlc said 2026-10-29 its ETA is 19:00 EDT.` → none; `Phase 7; postmaster-unit's reply record (main abc8344f)` → 7; `Phase 19 (numbering only)` → 19; `Phase 10b, Phase 11` → 10b, 11; `none` → none. `phase_table.py parallel` on hierarchies-unit's plan prints no `Phase 2026`. The `scripts.direct.test_phase_table` and `scripts.direct.test_progress_history` suites stay green.

### Phase 17 — Five rerun faults in the unit scripts clear on a rerun  · status: todo
**Depends on:** Phase 16 (the sweep list) and Phase 14 (order only)

The showrunner routed five rows of the rerun sweep (`docs/plans/sdlc-rerun-sweep.md`, checked at sdlc 097b1a0d) to phases-unit on 2026-10-29: the four rows in its phases-unit section and `scripts/direct/end_session.sh:87` from the fix-rounds section (fix-rounds-unit is stopped). Each row is a step that, if stopped at the named line, leaves a state that a rerun cannot get past or reads wrong. Line numbers are from 097b1a0d; find each by the code the row names.

#### Work Order

**Goal:** after a stop at each named line, the next run of the same command finishes the work and reports the true state.

**Spec:**
- `scripts/direct/assign.py` (claim check, row line 363): a launcher claim records the launcher's process start time beside its pid. A claim is live only when a process with that pid exists and has the same start time; a reused pid reads as a stale claim, and the rerun takes the worker. A claim with no start time (written before this change) keeps today's pid-only check.
- `scripts/direct/findings.py` (`landed`, row line 1218): a stop after the state write and before the `finding_batch_landed` event no longer leaves history without the event. A rerun of `landed` for that round writes the missing event once and says so; a round whose event exists stays "no repair round in flight".
- `scripts/direct/verify.sh` (lint pass cache, row line 2065): a recorded failure on a tree is never hidden by an older recorded pass for the same tree. A forced lint that fails deletes the old pass record before it publishes its failure record, so a stop between the two steps leaves no record and the next call runs lint again.
- `scripts/direct/verify.sh` (resource wait close, row line 1157): when closing a recorded resource wait fails, cleanup keeps the wait's id where the next run finds it (or keeps the folder), and the next run closes that earlier wait before it opens its own.
- `scripts/direct/end_session.sh` (row line 87): one run whose `roles.py unbind-run` fails no longer stops the cleanup of the other runs in the list. Each run is unbound and cleaned on its own; the script exits nonzero at the end and names each run that failed, so a rerun retries only those.

**Files:**
- `scripts/direct/assign.py`
- `scripts/direct/test_assign_launcher.py`
- `scripts/direct/findings.py`
- `scripts/direct/test_findings.py`
- `scripts/direct/verify.sh`
- `scripts/direct/test_verify_hold_lint.py`
- `scripts/direct/test_verify_hold_wait.py`
- `scripts/direct/end_session.sh`
- `scripts/direct/test_end_session.py`

**Workers:** `2 impl` (one for `assign.py`, `findings.py`, `end_session.sh` and their tests; one for `verify.sh` and its two test modules).

**Acceptance gate:** for each of the five rows, one test stops the command at the named line (a fault injected at that step), reruns the same command, and asserts the rerun's result: a reused pid takes the worker; `landed` restores `finding_batch_landed` exactly once; a failed forced lint after a saved pass reports the failure on the next ordinary call; a failed wait close is closed by the next run; a failed unbind still cleans the other runs and the rerun retries only the failed one. Each test fails on the tree before its fix. The `scripts.direct.test_assign_launcher`, `scripts.direct.test_findings`, `scripts.direct.test_end_session`, `scripts.direct.test_verify_hold_lint` and `scripts.direct.test_verify_hold_wait` suites stay green.

### Phase 18 — The Stop hook lets a full-context stop through only when something live will wake the session  · status: done
**Depends on:** none (showrunner fix, 2026-10-29: ahead of other work)

The showrunner sent a wrong result (2026-10-29): accounts-unit ended its turn at 17:39:47 EDT at 98% context with "— blocked: my context is full; it compacts on the next turn". No request followed, so it sat stopped 3.5 h. `scripts/hooks/stop-direct-continue.py` (Phase 6) let the stop through. The return that fired is `workers_working(session_dir)`: it reads only the age of `heartbeat.log`, and the launcher wrote "agent finished" to that log at 17:39:38, 9 s before the stop. A finished worker writes the heartbeat too, so a fresh heartbeat is not a live wake source.

#### Work Order

**Goal:** at or above the handoff threshold, the hook lets a stop through only when a live wake source exists; otherwise it blocks once, so the next request runs compaction.

**Spec:**
- `scripts/hooks/stop-direct-continue.py` measures context before any wake-source check. Below the threshold, or when context cannot be measured, the stop passes, as now.
- At or above the threshold, the only wake source is a live entry in the payload's `background_tasks` (`pending_background_tasks`): a launcher, a verification or an Agent subagent the CLI will report when it ends. The `heartbeat.log` age no longer counts; `workers_working` leaves the hook, and `direct_run.py` drops it if no other caller remains.
- The other early returns keep their meaning: a subagent, no session id, no active run, a declared user or supplier wait (`record_turn_wait`), and `stop_hook_active` (block once only).

**Files:**
- `scripts/hooks/stop-direct-continue.py`
- `scripts/hooks/direct_run.py`
- `scripts/hooks/test_stop_direct_continue.py` — new

**Workers:** `1 impl`.

**Acceptance gate:** `scripts/hooks/test_stop_direct_continue.py` has one test per return in `main()`, each feeding a payload on stdin and reading the hook's output. One test writes a fresh `heartbeat.log` whose last line is "agent finished", sets context above the threshold, gives no live background task, and asserts a block; it fails on the tree before the fix. Another gives one running background task above the threshold and asserts no block. The `scripts.direct.test_phase_artifacts` suite stays green.

### Phase 19 — An uncertain wait reason prints once, with its count  · status: todo
**Depends on:** none (showrunner add-on from hierarchies-unit, 2026-10-29)

`progress_history.py` (near line 5869) joins one reason per uncertain wait with "; ", so an `UncertainCoverage` cell repeats "worker hana_catalyst-lfo-impl6 has 0 active pass matches" five times.

#### Work Order

**Goal:** an `UncertainCoverage` phase's reason names each distinct reason once.

**Spec:**
- `scripts/direct/progress_history.py` writes each distinct uncertain-wait reason once, in first-seen order, joined with "; ". A reason that repeats gets " (<n> times)" after it; a reason seen once has no count. `candidates` and `attribution_miss_ids` stay as they are.

**Files:**
- `scripts/direct/progress_history.py`
- `scripts/direct/test_progress_history.py`

**Workers:** `1 impl`.

**Acceptance gate:** a test with five uncertain waits that share one reason and one wait with another reason gets "<reason> (5 times); <other reason>". `~/.claude/scripts/lib/py ~/.claude/scripts/hierarchies/measure.py baseline --full --since 2026-10-29T03:05:59Z --until 2026-10-30T00:00:00Z`, run against this tree, shows the hana_catalyst-lfo-impl6 reason once. The `scripts.direct.test_progress_history` suite stays green.
