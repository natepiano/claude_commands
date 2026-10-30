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

**Gotchas:** `test_phase_process.py` holds 3 tests that read the design doc's Recommendation, Comparison and Steps; an edit there must keep them green. Since Phase 14, `phase_table.py` reads a `**Depends on:**` line as phase ids only from `Phase <id>` spans of this plan; other conditions go on a `**Waits for:**` line. `scripts/guilds/daily_read.py` needs finish-time selection from `phase_boundaries`.

**Ruled out:** a retrospective before sample — the design's Steps need a prospective one; start-time selection inside `phase_boundaries` — it breaks `daily_read.py`; deploy time as exposure — it does not prove which contract a phase read.

### Phase 4 — `direct.md` becomes a short route, and each step reads only its own rule file  · status: todo
**Depends on:** Phase 3, Phase 6, Phase 7, Phase 8, Phase 9, Phase 11, Phase 12, Phase 13, Phase 17, Phase 20

**Waits for:** Phases 3, 6, 7, 8, 9, 11, 12, 13, 17 and 20 on main (17 and 20 change the launcher and wait records the comparison holds fixed), and postmaster-unit's write-once reply records on main (the showrunner names each hash and time); then at least twenty eligible `BeforeChange` phases (`--before-at` main HEAD) from at least two units after the latest of those times, so both samples run with the same recorder, wait records, launchers and `phase_process.py`. fix-rounds-unit's `<FixDispatch/>` edits to `commands/unit/direct.md` are on the sdlc merge branch since c79e5d12.

#### Work Order

**Goal:** the before sample is frozen in the Step log, then `commands/unit/direct.md` is a short route and each step's rules live in one file that the director reads at that step.

**Spec:**
- First, before any edit to a contract file, freeze the before sample. Fix both bounds: `--since` is the latest of the main times named in **Waits for**, and `--until` is the freeze time, written as UTC stamps in the Step log. Run one invocation per unit: `phase_process.py baseline --exposure before --before-at <~/.claude main HEAD at the freeze> --units <one unit> --since <the latest main time> --until <the freeze time>`. `render_trial` pools the primary measures across all selected units, so a run with several units gives no per-unit result. Run each unit again with `--detail trial` to list its eligible rows. Record in the design doc's **Step log** the `phase_process.py` commit, the before anchor, the filter, each unit, the window, n for each measure, each excluded state with n, median and p90 phase time per unit over the trial's eligible phases (the trial section's phase-time line), and the selected phase instances.
- Phase instances and matching metadata: the detail table prints only run and display phase id. For each detail row, read its phase start event in `~/.local/state/plan-delegate/runs/<run>.jsonl` (read-only) and record the run id, display phase id, `phase_instance_id`, start time, mode, scope class, worker model and gates. When one display id has more than one instance in the run, the start time inside the window selects the instance; when that does not select exactly one, the Step log names the row ambiguous. The phase start event does not hold mode, scope class, worker model or gates today: before the freeze, find each field's source (the run start event, the phase's `pass_started` records, its retained prompts or check artifacts) and name it in the Step log. Scope class is the number of paths in the phase's Work Order **Files** list: 1–5 small, 6–20 medium, more than 20 large. A field that no source holds is `NotRecorded` for that row, never a blank. Phase 5 matches on these recorded values.
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
- Matching: the same units in both windows, with matched mode, scope class, worker model and gates. If they do not match, the result is `inconclusive`, with what did not match; it is never read as a pass or a failure. Two `NotRecorded` values never match: a control that is `NotRecorded` in either window makes the result `inconclusive`.
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

**Constraints from prior phases:** Deployed revisions: Phase 3 is merged as b5187294, and ~/.claude main has been bc9e8ad9 since 05:29 EDT 2026-10-29. Phase 6, Phase 7, Phase 8 and Phase 4's route are pending: the showrunner names hash and time for each, and the Step log records them. The Step log before measure (version, filter, units, n, fixed window, phase instances with matching metadata), the deploy hash and time from the showrunner, and the change Phase 3 shipped. Step 1's target: weighted director tokens 3–7% lower per phase plus gap; gap 0–3 s. Exposure comes from the `direct.md` hash the recorder records at each phase start and finish (Phase 3), never from a deploy time; Phase 4 froze the before sample before the route. The next step is chosen from B, full A, G and I (design doc, Recommendation); C waits until dispatch time comes first. Phases 6, 7, 8, 9, 11, 12, 13, 17 and 20 and postmaster-unit's write-once reply records are on main before the before sample starts, so both samples use the same recorder, wait records, launchers and `phase_process.py`. A change to the recorder, a launcher or `phase_process.py` that reaches main between Phase 4's freeze and this collection makes the result `inconclusive`, naming the change, unless a new before sample is frozen after it. `test_phase_process.py` holds three tests that read the design doc's Recommendation, Comparison and Steps; every Step log or Recommendation edit keeps them green.

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

#### As-built

- `progress_history.py` `_supplier_message(identity, prompt, owner_session)` returns `VerifiedSupplierMessage | UnverifiedSupplierMessage`, and `finish_turn_wait` passes `owner_session` to it. A verified message names its route: `SupplierMessageRoute.DIRECT_REPLY` or `SupplierMessageRoute.POSTMASTER_REPLY`.
- A postmaster forward ends a `WaitingForSupplier` wait only when every check passes, in this order: the outer sender resolves to the postmaster's own address (checked before any record is read); the forward text has the postmaster's `[<at_ms>-<n>]` form; exactly one valid record in `$XDG_STATE_HOME/message/postmaster/replies/<at_ms>-*.json` has that `at_ms`, a `socket` equal to the supplier's current address, a `body` equal to the forwarded body, and this session in `targets`.
- Each record is parsed once into a typed `PostmasterReplyRecord` (frozen dataclass; `from_fields` raises `ValueError` on a bad field). A symlink, a bad file name or an unreadable record is skipped. The reader's current form is in the phase "The postmaster reply reader reads write-once records".
- A failed check gives `UnverifiedSupplierMessage` with a `SupplierMessageRejection` (such as `OUTER_SENDER_NOT_POSTMASTER`, `NO_RECORD`, `SEVERAL_RECORDS`, `SOCKET_MISMATCH`, `BODY_MISMATCH`, `NOT_A_TARGET`), and the wait stays open. A direct supplier reply ends the wait as before.
- A verified reply alone never ends `WaitingForUser`; that still needs the expected showrunner's relay.

**Files:**
- `scripts/direct/progress_history.py` — forwarded-reply verification and the rejection enum
- `scripts/direct/test_progress_history.py` — pending-forward, rejection and direct-delivery tests
- `scripts/hooks/test_direct_turn_wait_hooks.py` — hook-level wait tests

**Gotchas:** the record file counter (`<n>` in the file name) is not the forward's suffix counter; the match uses `at_ms` only. `targets` proves the intended receiver while the forward is still pending; the confirmed ids are never part of the match.

**Ruled out:** checking a forwarded reply against a log that the postmaster writes after it forwards — the recipient's hook runs before that log exists.

### Phase 9 — A decision that makes a run useless cancels that run in the same turn  · status: done
**Depends on:** none

#### As-built

- `<WorkerRuns/>` in `commands/unit/direct.md` holds the run-cancel rule: when a decision makes a running or queued test, lint or build run useless (the phase is parked, the round failed, a newer tree replaces the run's tree, or a result in hand answers the question), cancel it in the same turn to free its memory and its place in the queue. No run finishes only to complete a report.
- Cancel steps by run kind. A `mesh_runs.py` run: read `pid` from its record in `${SESSION_DIR}/mesh_runs/` (the `.json` beside its log); signal only while `status` is `running` and `ps -p <pid> -o args=` shows `mesh_runs.py supervise` with that record's path, so a reused pid is never signalled; then `kill -TERM -- -<pid>`. A detached `verify.sh` run: the same record and command, because `verify.sh` detaches through `mesh_runs.py` and handles TERM. A run in the memory queue: cancel its owning run; the queue drops dead callers on its next read. The queue has no cancel command of its own (`memory_admit.py release` only releases a reservation); the rule says so and points to `docs/plans/sdlc-run-cancel-design.md`.
- The park path and the repair-round path in `direct.md` point to `<WorkerRuns/>`. In `/showrunner:produce`, a ruling that parks, stops or replaces work names the runs to cancel.
- `docs/direct/write_prompt_contract.md` item 3 (`## Builds and tests`) holds the worker form: cancel a run of your own that a later edit or result makes useless, with the same pid check and `kill -TERM -- -<pid>`.

**Files:**
- `commands/unit/direct.md` — the `<WorkerRuns/>` rule and its park and repair pointers
- `commands/showrunner/produce.md` — the ruling rule
- `docs/direct/write_prompt_contract.md` — the worker sentence in item 3

**Binds later work:** the short route keeps the complete `<WorkerRuns>` body, with this rule, in the always-read part of `direct.md`, and the worker sentence stays in `write_prompt_contract.md` item 3. Both texts and their park and repair references stay byte-identical across the move.

### Phase 10 — Findings and a design: checkable cancel criteria and one stop command  · status: done
**Depends on:** none

#### As-built

- `docs/plans/sdlc-run-cancel-design.md` holds findings and a design. No code changed.
- Findings: for each of the five cancel criteria (phase or reservation released, parked or failed; tree no longer the tip of a live branch or phase; unit recorded a stop or park; owning session gone; a newer run queued for the same phase and step), the doc names the source a tool reads, its reliability today and the check that closes the gap, with live natedev observations. No criterion is reliable today as an automatic trigger.
- Design: one proposed command, `scripts/direct/run_stop.py`, with command, round and phase forms. It writes one atomic stop record per run and a findings-ledger projection for a stopped round; exit 77 always means cancelled; one cancel id covers one command; the cleanup order releases a memory charge only when the owned execution is confirmed empty. The doc names each run kind's run-to-phase link today, the link it must gain, and the callers (director, showrunner, repair round). `run_stop.py` is a proposal, not a plan phase; it does not exist.
- The `verify.sh`, board and memory-queue part is marked as builds' part, for builds to agree.

**Files:**
- `docs/plans/sdlc-run-cancel-design.md` — findings, design, builds' part, open questions

### Phase 10b — The run-stop design carries its owners' answers  · status: done
**Depends on:** Phase 10

#### As-built

- `docs/plans/sdlc-run-cancel-design.md` holds each owner's answer to builds' part. The design defines no copy path. It points to the user's rule of 2026-10-29 (`/tmp/claude/copies/<showrunner>-<unit>/<phase>-<purpose>`, the folder name from `roster.UnitLabels.folder_name()` on labels from `roster.unit_labels_for`, owned by verify-unit's `docs/plans/verify-copies.md`), and the run-stop provenance goes in a file inside that folder. `board.sh` is natedev's file.
- verify-unit agreed items 1, 3, 4 and 6: the launchers export the owner link before `verify.sh` refuses calls without it; one command id passes down in the environment; the TERM order builds on verify-unit's token fix (no reclaim from a live holder, token renewed with `--pid`); the Mac claim stays until the remote side is empty; one 15 s attempt deadline; only verify's own cancel path sets exit 77.
- resources-unit agreed item 2: charges bind to the step's systemd scope cgroup; a dead wrapper's kept charge counts measured memory; a step with no scope keeps today's rule; hold waits cancel through `interrupt-local`; a stopped command leaves a tombstone.
- Items still open stay under the doc's Open questions. No code changed.

**Files:**
- `docs/plans/sdlc-run-cancel-design.md` — the owners' answers

**Ruled out:** a second copy path rule in the design — the user's copies rule is the only one.

### Phase 11 — Each quota, memory and slot wait names who holds what it waits on  · status: done
**Depends on:** Phase 8

#### As-built

- `progress_history.py` holds a tagged holder: `NamedHolder(resource_kind, resource, holder)`, with `resource_kind` one of `account`, `slot`, `memory` and `holder` either `SessionHolder(session_id)` or `UnitHolder(unit)`; or `HolderNotNamed(reason)`. Each is a frozen dataclass that checks its fields; no empty string or null carries state.
- `start-wait` takes `--holder-resource-kind`, `--holder-resource` and one of `--holder-session` or `--holder-unit`, all or none. None records `HolderNotNamed("producer named no holder")`. A partial set, or holder flags on a kind other than `WaitingForQuotaRefusal`, `ReadyWithoutCapacity` and `WaitingForMemoryOrSlot`, exits 2.
- `wait-holder --session-dir D --wait-id ID <holder flags>` appends a `wait_holder_seen` event for an open wait, and exits 2 for a closed or unknown wait id.
- `waits` gives each wait `holders`: every holder seen, in order, each with its time. A record with no holder events reads `HolderNotNamed("recorded before holders")`.
- A wait event row with no `wait_id` counts in `ignored_n` and in the top-level `wait_without_id_n`. Its phase (same `run_id` and `phase_instance_id`) reads `HistoryNotRecorded` with reason `wait record has no wait id`.
- Each phase row carries `plan_doc`, `production` and `repository` as `{"state": "Recorded", "value": ...}` or `{"state": "NotRecorded", "reason": ...}` (`RecordedPhaseMetadata | PhaseMetadataNotRecorded`). `phase_started` records `production`, `unit` and `repository`. An older event resolves them once per run from disk: the plan header's `> **Production:**` line, with or without a unit part, and the worktree's git common directory.

**Files:**
- `scripts/direct/progress_history.py` — holder types and flags, `wait-holder`, the `holders` output, the wait-without-id count, phase source metadata
- `scripts/direct/test_progress_history.py` — CLI tests for each case

**Binds later work:** the producers are other units' files and have hand-offs: `verify.sh`'s `phase_wait_start` (builds) passes the board token owner for a slot wait, the memory reservation owner for a memory wait and the build hold's holder for a hold wait, and calls `wait-holder` when the holder changes; `codex_mesh.py`'s `_mesh_phase_wait` (models-unit) passes the account label for quota and capacity waits. hierarchies-unit reads the holder field shape.

**Gotchas:** most plan headers name the production with no unit part, so the production reader accepts both header forms.

### Phase 12 — The postmaster reply reader reads write-once records  · status: done
**Depends on:** Phase 8

#### As-built

- `PostmasterReplyRecord` holds `at_ms`, `socket`, `name`, `session_id`, `senders`, `body` and `targets`; it has no `forward` or `forwarded` field. A record written before the write-once change still carries those fields; the reader does not read them, and the record reads the same.
- `_read_postmaster_reply_record` reads a record once, with no re-read and no sleep. Unreadable or invalid JSON is a rejected record.
- The reader does not read `forwards/`: `targets` decides intent, and no check uses the confirmed ids. The match rules, rejections and wait ending of the phase "A supplier reply that the postmaster forwards ends its wait" are unchanged.

**Files:**
- `scripts/direct/progress_history.py` — the reply record type and its single read
- `scripts/direct/test_progress_history.py` — write-once, old-form and rejection tests

**Gotchas:** the postmaster writes `replies/<at_ms>-<n>.json` once before the first forward, and `forwards/<at_ms>-<n>.json` (`{forwarded}`) once after the last. No file is rewritten, so a reply record read after the forward is complete.

**Ruled out:** re-reading a torn record — a write-once record cannot be torn.

### Phase 13 — The trial measures table fits a phone  · status: done
**Depends on:** Phase 11

#### As-built

- The Trial measures table in `phase_process.py baseline` keeps every line at most 60 characters. Counts print in short form (`55K`, `2.4M`, `12.3B`, `1.0T`; values below 1,000 as they are), and a value that rounds up to 1,000 of one step prints in the next (999,999 is `1.0M`).
- Row labels are short (`weighted tokens`, `raw input with caches`, `raw output`, `compactions`). A second heading line says director tokens are phase plus gap and compactions are per phase. The title does not repeat the window.
- The table has no not-recorded column: each measure with a not-recorded count above 0 prints one line under the table, such as `not recorded: weighted tokens n=1`.
- The other tables and the `--detail trial` rows keep full values. The shared `table()`, `distribution_row()` and `count()` are unchanged.

**Files:**
- `scripts/direct/phase_process.py` — the short-form trial table
- `scripts/direct/test_phase_process.py` — width, short-form and not-recorded line tests

**Binds later work:** the trial table cells are short counts (`2.7M`) that hide differences near 1.5%. A Step log records each eligible phase's exact weighted tokens from the `--detail trial` rows (`str(row.tokens.weighted)` in `phase_process.py`), never the table cells.

### Phase 15 — A rerun review lens checks every change that writes lasting state  · status: done
**Depends on:** none

#### As-built

- `docs/direct/dual_review.md` `<TeamReview/>` has a third lens, `rerun`, with five questions, each with one real case: (1) after a stop at any line, does a rerun finish; (2) does it trust a stored status without checking the live thing (pid and process start time, path, lock holder); (3) does a failure move or delete what a rerun needs; (4) does one failure stop other, unrelated steps (`set -e` over a list); (5) does each mark have an expiry or an owner check, and a command that clears it. A failed answer is a defect, fixed in the phase. The prompt closes with the rerun case the reviewer built, or a statement that it could not build one.
- Arming: `<DualReview/>` runs `rerun_lens.py arm --diff <diff>` on the completed phase diff, made with whole-file context (`git diff --unified=100000`), before the broad review. `rerun` is armed when the Work Order's `**State:**` line names lasting state or the check prints `armed`. `none` or an absent line never stops a detected write from arming it, and a check error is not `not armed`. When armed, `rerun` launches with the other lenses on the same review copy, and its result is `review_findings_<pass>_rerun.txt`.
- `scripts/direct/rerun_lens.py arm --diff <file>` prints `armed` with each hit as `path:line — <kind>`, or `not armed`; it exits 2 on a read or parse error. It reads only. Kinds: file write or append, file move or delete, lock or claim, JSON state write, and fail-fast step lists. It reads Python, shell (`mv`, `rm`, `> file`, `mkdir` as a lock, `flock`, `set -e` over a list) and Rust (`fs::write`, `File::create`, `OpenOptions` with write or append, `fs::rename`, `remove_file`, `remove_dir_all`, `fs2`, `fd-lock`). Test files, fixtures, docs and `#[cfg(test)]` items do not count.
- The lens list exists once: `scripts/usage/agents_log.py` `Lens` (a `Literal` that includes `rerun`) and `LENSES = get_args(Lens)`. `assign.py --lens`, `findings.py` (`REVIEW_LENSES`, `open --lens`) and `review.sh --lens` read it. In `review.sh`, `adversary` takes the `test` slot, `contract` takes `impl`, and every other lens, `rerun` included, takes `review`.
- `docs/direct_plan_format.md` and `docs/direct/compose_work_order.md` define the optional `**State:**` field after `**Workers:**`; `berth.work_order validate` accepts a Work Order with or without it. The `findings.py open` row in `commands/unit/direct.md` lists `rerun`.

**Files:**
- `docs/direct/dual_review.md` — the lens, its questions and cases, the arming step
- `docs/direct/compose_work_order.md`, `docs/direct_plan_format.md` — the `**State:**` field
- `commands/unit/direct.md` — the lens list in the `findings.py open` row
- `scripts/direct/rerun_lens.py`, `scripts/direct/test_rerun_lens.py` — the diff check and its fixture tests
- `scripts/usage/agents_log.py` — the one lens list
- `scripts/direct/review.sh`, `scripts/direct/assign.py`, `scripts/direct/findings.py` — read the lens list
- `scripts/direct/test_review_launcher.py`, `test_review_launcher_pool.py`, `test_review_launcher_delivery.py`, `test_assign_launcher.py`, `test_findings.py`, `test_review_pool_report.py`, `test_progress_history.py`, `scripts/history/test_rounds.py`, `scripts/usage/test_agents_log.py` — accept `rerun`

**Binds later work:** a new lens is one edit to `Lens` in `scripts/usage/agents_log.py`.

### Phase 16 — One rerun sweep of the scripts names each fault and its owner  · status: done
**Depends on:** Phase 15

#### As-built

- `docs/plans/sdlc-rerun-sweep.md` lists the places in `~/.claude/scripts` where a stop or a failure leaves state that a rerun does not repair. It was read at sdlc `097b1a0d` with the five `rerun` lens questions, over every script that `rerun_lens.py arm` marks armed on the whole file. Each row has `path:line`, the question number, the rerun result and the owner unit (from the file's last checkpoint subject; `unknown` when no checkpoint names the file). Rows are grouped by owner. The unit director checked each row against the tip. The sweep fixes nothing; the showrunner routes each row.
- `progress_history.py waits` keeps malformed holder evidence visible: a present `holder` that is not an object, or that has a field outside its state's schema, becomes `MalformedHolder` (`{"state": "MalformedHolder", "reason": ..., "raw_value": ...}`). Only an absent `holder` key reads `HolderNotNamed("recorded before holders")`. Terminal wait rows (`wait_finished`, `wait_unclosed`) that carry a holder are read the same way; a terminal row that repeats a holder already seen adds no observation.
- A phase's `production` in `waits` is a production name, never a path. A plan header or a stored `phase_started` value that names a production doc (a value with `/` or a `.md` suffix) is read through that doc's `# Production — <name>` heading. A doc that cannot be read or has no heading gives `PhaseMetadataNotRecorded` with the reason.
- `scripts/production/production_doc.py` (standard library only) has `production_name(lines) -> ProductionName | ProductionNameNotRecorded`. `production_lifecycle.production_name` reads through it and keeps its `LifecycleStop`. `progress_history.py` imports `production_doc` at module level, and no import cycle exists.

**Files:**
- `docs/plans/sdlc-rerun-sweep.md` — the sweep list, with its tip hash and time
- `scripts/direct/progress_history.py`, `scripts/direct/test_progress_history.py` — `MalformedHolder` and production names
- `scripts/production/production_doc.py`, `scripts/production/production_lifecycle.py`, `scripts/production/test_production_lifecycle.py` — the shared production name reader
- `scripts/direct/review_launcher_test_support.py`, `scripts/direct/implement_launcher_test_support.py` — their copied scripts tree holds `production_doc.py`

**Binds later work:** the rerun-fault rows routed to their owners come from `docs/plans/sdlc-rerun-sweep.md`, checked at `097b1a0d`; line numbers there are from that tip. A reader of `holders` that counts holder evidence treats `MalformedHolder` as not valid evidence and shows it.

**Gotchas:** a new import in `progress_history.py` must also go into the copied scripts trees of `review_launcher_test_support.py` and `implement_launcher_test_support.py`.

### Phase 14 — A plan's Depends line counts only this plan's own phases  · status: done
**Depends on:** Phase 15 (order only: the showrunner ranks the rerun lens first)

#### As-built

- `scripts/direct/plan_history.py` reads a phase's `**Depends on:**` ids only from `Phase <id>` and `Phases <id>, <id> and <id>` spans. The keywords match in any case; the id suffix is lowercase. An id followed by a word character, or by `-`, `:`, `.` or `/` and a word character, is not an id, so dates, times, hashes and durations (`2-week`) never count. Numbers outside a span (`Steps 1 and 4`) never count. `none` gives an empty list; an absent line stays `None`.
- A span that follows another unit's name (`models-unit's Phase 4`, also in backticks, quotes or bold) is that unit's phase and is skipped. A span that follows the plan's own unit name, read from its ``> **Production:** … — unit `<unit>` `` header, counts.
- `phase_table.py` reads `depends_on` unchanged, and a named id that is not a phase of the plan still prints `not in the plan`.

**Files:**
- `scripts/direct/plan_history.py` — the dependency parser (`PHASE_REFERENCE_PATTERN`, `UNIT_PHASE_PREFIX`, `PLAN_UNIT_HEADER`)
- `scripts/direct/test_plan_history.py`, `scripts/direct/test_progress_history.py` — dependency-parse tests

### Phase 17 — Five rerun faults in the unit scripts clear on a rerun  · status: done

#### As-built

- A stop at each of five named steps no longer blocks a rerun. Each fault has a stop-and-rerun test that fails on the tree before its fix.
- `assign.py`: a launcher claim records the launcher's process start time beside its pid. A claim is live only when that pid exists with the same start time. A reused pid reads as stale, and the rerun takes the worker. A claim with no start time keeps the pid-only check.
- `findings.py` `landed`: a rerun after a stop between the state write and the `finding_batch_landed` event writes the missing event once and says so. A round whose event exists stays "no repair round in flight".
- `verify.sh`: a failed forced lint deletes the old pass record before it publishes its failure record, so an older pass never hides a failure on the same tree. When a resource wait close fails, cleanup keeps the wait id (or the folder), and the next run closes that wait before it opens its own. The wait file is published by a staged write.
- `end_session.sh`: each run is unbound and cleaned on its own. A failed `roles.py unbind-run` does not stop the other runs. The script exits nonzero at the end and names each failed run, so a rerun retries only those.
- `scripts/lib/process_start.py` is the one process start and /proc stat reader under `scripts/`. `check_owner` and `check_group_leader` return `OwnerCheck` (ALIVE, GONE, REPLACED, UNKNOWN), computed per call and never stored. Each caller maps it to keep, reclaim or signal. Every per-script copy of a start reader is deleted.

**Files:**
- `scripts/lib/process_start.py` — the shared reader and `OwnerCheck`; `scripts/lib/test_process_start.py`.
- `scripts/lib/process_start_test_support.py` — the one fake stat helper for tests (`write_stat`, `process_start_probe`, `stored_start_probe`).
- `scripts/direct/assign.py`, `findings.py`, `verify.sh`, `end_session.sh` — the five rerun fixes; tests in `test_assign_launcher.py`, `test_findings.py`, `test_verify_hold_lint.py`, `test_verify_hold_wait.py`, `test_end_session.py`.
- Callers of the shared reader (own copy deleted): `scripts/direct/` (`step_record.py`, `tree_watch.py`, `board.sh`, `implement.sh`), `scripts/agents/codex_mesh.py`, `scripts/production/` (`retire_unit.py`, `merge_gate.py`, `fake_unit_host.py`, `rename_unit.py`), `scripts/herdr/herdr.py`, `scripts/message/sessions.py`, `scripts/shutdown/` (`remote_control.py`, `stop.py`, `inventory.py`, `pane_record.py`), `scripts/nix_env/heal.py`, `scripts/mac_test/` (`mac_test.py`, `copy_watch.py`), `scripts/build_targets/build_targets.py`, `scripts/build_hold/` (`build_hold.py`, `hold_enforcer.py`), `scripts/lint/` (`memory_admit.py`, `tmp_sweep.py`), `scripts/buildlog/mac_idle.py`. Their tests and test support modules use the shared fake stat helper.

**Binds later work:**
- Any code that reads a process start or decides a process is stale calls `scripts.lib.process_start` (`check_owner`, `check_group_leader`). Only GONE and REPLACED reclaim. Only ALIVE signals. UNKNOWN keeps the record.
- Tests fake process state through `scripts/lib/process_start_test_support.py`, never a local stat writer.
- "A stopped verify.sh wait is found and closed on a rerun" uses the staged wait-file publication in `verify.sh` and the shared reader for its owner check.

**Gotchas:**
- A Mac start time is `ps -o lstart=` text, read with `LC_ALL=C` and `TZ=UTC`. A host without /proc gives UNKNOWN on a date mismatch, never REPLACED.
- A test that patches a per-script start reader breaks, because those readers are gone. Patch `scripts.lib.process_start`.
- A stop inside `phase_wait_start` still leaves an open wait with no wait file. "A stopped verify.sh wait is found and closed on a rerun" closes it.
- A mesh inbox race in `codex_mesh.py` stays open; models-unit owns that file.

### Phase 18 — The Stop hook lets a full-context stop through only when something live will wake the session  · status: done
**Depends on:** none (showrunner fix, 2026-10-29: ahead of other work)

#### As-built

- `scripts/hooks/stop-direct-continue.py` measures context before any wake-source check. Below the handoff threshold, or when context cannot be measured, the stop passes.
- At or above the threshold, the only wake source is a live entry in the payload's `background_tasks` (`pending_background_tasks`). With none, the hook blocks once, so the next request runs compaction. The `heartbeat.log` age is not a wake source: a finished worker writes the heartbeat too. `workers_working` no longer exists in `direct_run.py`.
- The other early returns keep their meaning: a subagent, no session id, no active run, a declared user or supplier wait (`record_turn_wait`), and `stop_hook_active` (block once only).

**Files:**
- `scripts/hooks/stop-direct-continue.py` — context first, then background tasks
- `scripts/hooks/direct_run.py` — `workers_working` removed
- `scripts/hooks/test_stop_direct_continue.py` — runs the real hook once per return in `main()`, the finished-heartbeat block case included

**Ruled out:** a fresh `heartbeat.log` as proof of a later wake-up.

### Phase 19 — An uncertain wait reason prints once, with its count  · status: done
**Depends on:** none (showrunner add-on from hierarchies-unit, 2026-10-29)

#### As-built

- `progress_history.py waits` writes an `UncertainCoverage` phase's reason through `_counted_uncertain_reasons`: each distinct uncertain-wait reason once, in first-seen order, joined with "; ". A repeated reason gets " (<n> times)" after it; a reason seen once has no count. `candidates` and `attribution_miss_ids` are unchanged.

**Files:**
- `scripts/direct/progress_history.py` — `_counted_uncertain_reasons`
- `scripts/direct/test_progress_history.py` — the repeated-reason test

### Phase 20 — A stopped verify.sh wait is found and closed on a rerun  · status: done
**Depends on:** Phase 17 (its wait publication in `verify.sh`)

Phase 17's closure work found this fault (Astra review of F008, 2026-10-30): `phase_wait_start` in `scripts/direct/verify.sh` runs `progress_history.py start-wait`, and only after it returns does the shell save the new wait id in its wait file. The recorder writes the open `wait_started` event first. A stop between the two leaves an open wait in history with no wait file, so no rerun can find it, and `phase_wait_retry_pending` and `phase_wait_finish_all` never close it. Phase 17 did not change this order; the fault is older.

#### Work Order

**Goal:** after a stop at any point while a wait starts, the next run in that session closes the earlier wait, and history holds no open wait without a file that names it. Every wait publisher uses one recorder-owned operation for this.

**Spec:**
- One operation owns wait identity and publication. `scripts/direct/progress_history.py` makes the wait id, writes the wait file first (the staged publication Phase 17 uses for wait files), then appends the `wait_started` event, all in one place. `verify.sh` `phase_wait_start` and `progress_history.py` `record_turn_wait` (~5672, which writes its marker after `_record_wait` returns) both call it. No second wait-file writer and no second id format.
- `codex_mesh.py` (~3306) has the same event-before-marker order. codex_mesh is models-unit's: this phase does not edit it; the checkpoint notice names the shared operation and asks models-unit to move that caller to it, with its own interruption test.
- `start-wait` takes an optional `--wait-id`. A repeat call with an id that is already recorded writes nothing more and prints the same id. This holds for every recorded state of that id: `wait_started`, `wait_not_recorded`, finished, and closed as unclosed. A repeat keeps the original attribution and terminal state. Without `--wait-id` it makes an id as it does now.
- History reads give a typed answer, not an empty list: `RecordedWait`, `NeverStartedWait` (the history was read in full and holds no event for the id) and `WaitHistoryUnavailable` (unreadable or malformed history). `_run_events` (~2436) returns `[]` on a read failure and skips malformed records today, so an empty list never proves absence.
- `finish-wait` on a `NeverStartedWait` id exits with its own status and message ("never started"), not the "was not recorded" error. The shell removes a wait file on success or on that status. On `WaitHistoryUnavailable` or any other failure it keeps the file and prints a diagnostic.
- Abandonment is terminal. A "never started" close records the abandoned id under the recorder lock (~6497), and a later `start-wait` for that id, from a recorder child that outlived its killed shell, writes nothing and exits with the abandoned status.
- `waits` (~5798) shows one closure per recovered wait, keeps each terminal state, makes no interval for a never-started id, and keeps the recovery evidence when a close fails.

**Files:**
- `scripts/direct/verify.sh`
- `scripts/direct/progress_history.py`
- `scripts/direct/test_progress_history.py`
- `scripts/direct/test_verify_hold_wait.py`
- `scripts/direct/verify_hold_wait_test_support.py`

**Workers:** `1 writer + 1 tester`
- `impl` — `scripts/direct/verify.sh`, `scripts/direct/progress_history.py`: the shared wait-publication operation and its callers
- `test` — `scripts/direct/test_progress_history.py`, `scripts/direct/test_verify_hold_wait.py`, `scripts/direct/verify_hold_wait_test_support.py`: new interruption controls extend the shared fixture in `verify_hold_wait_test_support.py` (~144), never a second fixture

**Constraints from prior phases:** Phase 17: import the shared reader only as `from scripts.lib import process_start`; fake stat lines use `scripts/lib/process_start_test_support.py` (`write_stat`, `process_start_probe`); only GONE and REPLACED reclaim, only ALIVE signals, UNKNOWN keeps the owner. A rerun recovers waits only for the same session and physical worktree (`verify.sh` ~2260); keep that boundary. Keep Phase 17's live-owner protection, UNKNOWN retention, staged publication, probe suppression and failed-close recovery, and keep its stop-and-rerun tests green.

**Acceptance gate:** a test stops `phase_wait_start` after the wait file is written and before the event; the next run closes it and history holds no open wait. A second test stops after the event; the next run closes that wait once. A third runs `start-wait --wait-id` twice with one id, for a started, a not-recorded, a finished and an unclosed wait, and finds one record each with its first attribution. A fourth makes history unreadable and finds the wait file kept with a diagnostic. A fifth pauses a recorder child before its append, kills only its shell, runs recovery, then resumes the child, and finds no reopened wait. A sixth stops `record_turn_wait` between its event and its marker and finds the wait closed on the next turn. `waits` shows one closure per recovered wait and no interval for a never-started id. Each test fails on the tree before the fix. The `scripts.direct.test_verify_hold_wait` and `scripts.direct.test_progress_history` suites stay green.
