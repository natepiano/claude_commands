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
**Depends on:** Phase 2; the user's approval of Step 1 (given 2026-10-28, PH2: tokens first, cheap A)

#### Work Order

**Goal:** with the old `direct.md` unchanged, every phase records the contract revision it read, and `phase_process.py` reports the trial's three measures with known exposure, so a prospective before sample can start.

**Spec:**
- First merge the sdlc merge branch (it carries builds-unit's red fix at e90afb10 and roles-unit's `roles_audit.py check`).
- The approved step, from the design doc's Steps, verbatim: "Make `commands/unit/direct.md` a short route: the always-read core rules, then one line per step naming its rule file. Move each rule body into its step file as it is; repair rules stay fix-rounds-unit's text and `<Guild>` text stays guild-process-unit's. After compaction the director reads the core rules and the current step's file by tag name. Keep the tag inventory complete; add no `/unit:delegate` copy. Start after fix-rounds-unit's `<FixDispatch/>` edits merge. Target: director tokens 3–7% lower per phase; gap 0–3 s. Phase 4 runs the 20-phase trial named in Recommendation. Total: 2–4 work hours plus the trial's recording. Undo: restore the single `direct.md` from git; retain the trial records." This phase builds the trial's recording only; Phase 4 builds the route; Phase 5 runs the trial. The design doc's Steps say to deploy the recording with the old behavior and collect a prospective old-route sample first; this phase follows that order.
- Exposure record: `progress_history.py start-phase` and `finish-phase` each add to their event the contract that the director reads: the `~/.claude` main HEAD commit, the sha256 of the bytes of `commands/unit/direct.md` there, and the sha256 of each file its `<TagReferenceContract>` table names, read at that moment (a dirty file is hashed as it is on disk). The recorder reads these itself; no step in `direct.md` changes. Fixture tests cover a clean tree, a dirty file and a missing file (recorded as a named missing state, never an empty hash).
- In `phase_process.py`, define `TrialExposure` as `BeforeChange`, `AfterChange` or `ExposureNotRecorded(reason)`. A phase is `BeforeChange` or `AfterChange` only when its start and finish records name the same `direct.md` hash and that hash is on the named side: `BeforeChange` when it equals the bytes of `commands/unit/direct.md` at a commit at or before the before anchor (a `--before-at <commit>` option, usable before any route commit exists), `AfterChange` when it is on the route side of the route revision (a `--route-revision <commit>` option); a hash on both sides, or on no side the options name, is `ExposureNotRecorded`; a phase with no record, or with different hashes, is `ExposureNotRecorded` with the reason. No deploy time stands for exposure.
- Eligibility, with a fixture test for each case: select phases by start time; a phase plus its following gap is eligible only when the next phase in the same run starts with the same `direct.md` hash. A phase with no following phase is `NoFollowingPhase`; two phases open at once with one director are `SharedDirector`. Both are counted apart with n and kept out of the phase-plus-gap measures; mixed phases stay out as today.
- The three measures per eligible phase, each with fixture tests:
  - **Weighted director tokens over the phase plus its following gap** — the primary measure. `read_usage` keeps the token classes (input, 5 m and 1 h cache writes, cache reads, output) and the model apart, and the weight comes from `scripts/usage/weights.py` (`weighted`, dated by turn time). A model with no weight is a named unweighted state, never 0. Raw input and output are reported apart.
  - **Compactions per phase** — `compact_boundary` entries in the director's transcript inside the phase interval.
  - **Context size after each reload** — for each compaction, the raw input (caches included) of the first request after the tool result of the director's first Read of `commands/unit/direct.md` (or, after the route, its step file) that follows the boundary. A phase with no compaction is `NoCompaction`; a compaction with no contract Read after it is `NoReload`; a missing transcript is `NotRecorded(reason)`; none of them is 0.
- Add `--units <a,b>` and `--exposure before|after` filters; windows are `[since, until)` in the user zone.
- Add `scripts/direct/tag_inventory.py` with a fixture test, run on today's tree. Scope: `/unit:direct`'s resolved contract — `commands/unit/direct.md`, every file its `<TagReferenceContract>` table names, and every file those name. In that scope each `<Tag/>` reference resolves to exactly one body: a table row or stub must name an existing file that holds the body. Fenced code, quoted examples and plan docs are not definitions; other commands' own tags (for example their own `<Guild>`) are out of scope. Fail on a missing body, two bodies, a stub or row whose file is gone or holds no body (stale path), and a forwarding cycle; one fixture each. It passes on today's tree.
- In the design doc, change the two "Phase 4 runs the 20-phase trial" lines (Recommendation and Step 1) to Phase 5, and add to Step 1: the before sample is prospective, from the deploy of this phase's recording; Phase 4 freezes it and then builds the route.

**Files:**
- `scripts/direct/progress_history.py`, `scripts/direct/test_progress_history.py` — the exposure record
- `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py` — `TrialExposure`, eligibility, the three measures, the filters
- `scripts/direct/tag_inventory.py`, `scripts/direct/test_tag_inventory.py` — the tag check (new)
- `docs/plans/sdlc-phases-design.md` — Step 1 and Recommendation lines

**Workers:** `2 writers` — the recorder and the measure are disjoint.
- `impl` — `scripts/direct/progress_history.py`, `scripts/direct/test_progress_history.py`, `scripts/direct/tag_inventory.py`, `scripts/direct/test_tag_inventory.py`
- `test` — opens as impl — `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py`, `docs/plans/sdlc-phases-design.md`

**Constraints from prior phases:** Phase 2 did the rounds.py swap: phase boundaries and fix time come from `scripts/history/rounds.py`, an open phase is a named state, and fix time is worker time. `phase_boundaries` (`phase_process.py`) selects phases by finish time and `measure_director_tokens` stops at phase end; this phase moves trial selection to start time and adds the gap. `read_usage` today sums input and every cache class into one number and drops the model. `direct.md` costs about 41k tokens as a Read (35k as raw text). `progress_history.py` is the recorder every unit runs: a change to its events keeps every reader of the run files working (`scripts/history/history.py`, `rounds.py`), with a test for an old event without the new fields. `progress_history.py` and `phase_process.py` belong to build-followups owners as well: the checkpoint notice names each owner of a touched file, trial-merged against its tip.

**Acceptance gate:** the gates in Delegation Context green, `basedpyright scripts/direct` included; `tag_inventory.py` passes on today's tree and each fixture fails as named; a live `start-phase`/`finish-phase` on a scratch run records the contract hashes; `phase_process.py baseline --exposure before` prints the three measures with n and each excluded state with n; the checkpoint is verified and mergeable (the showrunner merges it and moves it to main and names the time).

### Phase 4 — `direct.md` becomes a short route, and each step reads only its own rule file  · status: todo
**Depends on:** Phase 3 on main (the showrunner names the hash and time); at least 20 eligible `BeforeChange` phases (`--before-at` main HEAD) from at least two units after that time; fix-rounds-unit's phase 5 (its `<FixDispatch/>` edits to `commands/unit/direct.md`) merged to the sdlc merge branch

#### Work Order

**Goal:** the before sample is frozen in the Step log, then `commands/unit/direct.md` is a short route and each step's rules live in one file that the director reads at that step.

**Spec:**
- First, before any edit to a contract file, freeze the before sample: run `phase_process.py baseline --exposure before --before-at <~/.claude main HEAD at the freeze> --units <the units>` and record in the design doc's **Step log** the `phase_process.py` commit, the before anchor, the filter, the units, the window, n for each measure, each excluded state with n, and median and p90 phase time per unit.
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

**Constraints from prior phases:** Phase 3's recorder records the `direct.md` hash at each phase start and finish, and `phase_process.py` reads exposure from it with `--before-at` (before the route exists) and `--route-revision`. Cheap A's guards (design doc, Recommendation): it does not rely on the 20k restore after compaction, the core rules are always read, each tag is found by its name, the tag inventory stays complete, and no `/unit:delegate` alias copies the text. Move each rule body as it is: repair rules stay fix-rounds-unit's text, `<Guild>` text stays guild-process-unit's (user, 2026-10-28). `direct.md` is a build-followups hub file: the checkpoint notice names each build-followups owner of a touched file, trial-merged against its tip. roles-unit renamed 9 "seat" words to "worker" in `direct.md`; keep them.

**Acceptance gate:** the gates in Delegation Context green; the Step log holds the frozen before sample; `tag_inventory.py` passes before and after with the same tag list; `roles_audit.py check` prints no issue; each moved body is byte-identical to its source; the checkpoint is verified and mergeable.

### Phase 5 — Measure the first step and propose the next  · status: todo
**Depends on:** Phase 4, the showrunner's word naming the hash and time the route reached main, and at least 20 eligible `AfterChange` phases from the same units as the before sample

#### Work Order

**Goal:** the design doc says whether Step 1 moved its measure, and the next step goes to the user.

**Spec:**
- Run `phase_process.py baseline --exposure after --route-revision <Phase 4's route commit> --units <the before sample's units>` at the Step log's measurement version, and add the after numbers to the Step log with n for each: weighted director tokens over the phase plus its following gap (the primary measure), compactions per phase and context size after each reload (reported, no target). Show each unit's results apart, and each excluded state (`ExposureNotRecorded`, `NoFollowingPhase`, `SharedDirector`, mixed) with n.
- Matching: the same units in both windows, with matched mode, scope class, worker model and gates. If they do not match, the result is `inconclusive`, with what did not match; it is never read as a pass or a failure.
- Quality held: median and p90 phase time per unit do not rise against the Step log's before sample. A rise is reported with the step's result.
- If the window holds no eligible observation (n 0, or below the n the step names), the result is `not yet measurable`: the Step log names the missing evidence and the condition for the next collection, and the phase waits on it; this is never read as a failed step.
- If the primary measure did not fall by at least 1.5% (half the low end of the 3–7% target), the Step log says so, names the likely reason, and proposes undoing it or changing it; that proposal is the question.
- Otherwise update Steps with what the measure taught, and pick the next step.
- At phase end, the unit director sends the showrunner the result and the next step as one question for the user. The next approved step becomes a new phase after this one, with the Work Order of Phase 3 and the step's own Spec.

**Files:**
- `docs/plans/sdlc-phases-design.md` — Step log, Steps

**Workers:** `1 writer + 1 tester` — one doc. `impl` runs the measure and writes the Step log.
- `impl` — `docs/plans/sdlc-phases-design.md`
- `test` — opens as test — independently re-runs the frozen measure and checks the Step log; it writes no doc text

**Constraints from prior phases:** the Step log before measure (version, filter, n, window), the deploy hash and time from the showrunner, and the change Phase 3 shipped. Step 1's target: weighted director tokens 3–7% lower per phase plus gap; gap 0–3 s. Exposure comes from the `direct.md` hash the recorder records at each phase start and finish (Phase 3), never from a deploy time; Phase 4 froze the before sample before the route. The next step is chosen from B, full A, G and I (design doc, Recommendation); C waits until dispatch time comes first.

**Acceptance gate:** the Step log has before and after numbers with n and window, the matching result and the phase-time check; the question is sent.

### Phase 6 — Each phase records its waits and their cause, and each merge leaves a receipt  · status: todo
**Depends on:** Phase 3 (its recorder changes). It does not wait for Phase 4's before sample or fix-rounds-unit's phase 5, so it runs while Phase 4 waits; the number keeps the accepted Phase 4 and Phase 5 names stable.

#### Work Order

**Goal:** hierarchies-unit's approved Step 2 (HI7, a 4-week experiment): every new wait in a phase has a record with its cause, every merge has a receipt, and a missing record is a named state, never 0. No gate changes.

**Spec:**
- Source: `build-followups-hierarchies:docs/plans/build-followups-hierarchies-design.md` Steps item 2, and hierarchies-unit's record shape (2026-10-28). Research at phase start confirms each emit point below against the live scripts before the prompts are written.
- Wait record: `progress_history.py` gets `start-wait --kind <kind> [--reason <text>]` and `finish-wait`, and each event carries the phase id, unit, owner session, and UTC start and end. Kinds: `WaitingForQuotaRefusal` (only an actual provider refusal, never a quota reading), `WaitingForMemoryOrSlot`, `WaitingForMergeGate`, `WaitingForUser`, `WaitingForSupplier`, `ReadyWithoutCapacity`. A probe session (`accounts.py exec --probe`) writes no wait record. An open wait at `finish-phase` closes as an unclosed wait with its start, never with an invented end.
- Emit points, each a script that already sees the wait, so no `direct.md` step changes: the launcher on a provider refusal; `verify.sh` while it waits for memory admission, the cargo token or a build hold; the merge gate run; a turn that ends on `decision:` or `gate:` until the next user message; a turn that ends `blocked: waiting on the showrunner` or a peer until its message; a ready dispatch held for a worker or account. When the research finds no script that sees a wait kind, the checkpoint notice names that kind, and the phase builds no emit point for it.
- Merge receipt: each merge that `scripts/production/merge_checkpoint.py` makes writes one receipt under the history root: project, issue or plan phase, commit, target branch, and merge time in UTC. The receiving showrunner checks it with a read verb that names a merge commit with no receipt `ReceiptMissing`.
- Readers: a read verb returns waits and receipts for `[since, until)` with covered n and missing n; a phase in the window from a run with no wait history is `HistoryNotRecorded`, never 0. Old run files without the new events stay readable by `scripts/history/history.py` and `rounds.py`, with a test for an old event.
- Fixture tests: each wait kind, a probe session, an unclosed wait, an old run file, a merge with and without a receipt.

**Files:**
- `scripts/direct/progress_history.py`, `scripts/direct/test_progress_history.py` — wait events and the read verb
- `scripts/direct/implement.sh`, `scripts/direct/review.sh`, `scripts/direct/verify.sh` — wait emit points
- `scripts/production/merge_checkpoint.py`, `scripts/production/test_merge_checkpoint.py` — merge receipts

**Workers:** `2 writers` — the recorder and its readers, and the emit points with receipts.
- `impl` — `scripts/direct/progress_history.py`, `scripts/direct/test_progress_history.py`
- `test` — opens as impl — `scripts/direct/implement.sh`, `scripts/direct/review.sh`, `scripts/direct/verify.sh`, `scripts/production/merge_checkpoint.py`, `scripts/production/test_merge_checkpoint.py`

**Constraints from prior phases:** Phase 3 added the contract hashes to `start-phase`/`finish-phase`; keep that event shape. `progress_history.py`, `merge_checkpoint.py` and the launchers belong to build-followups owners as well: the checkpoint notice names each owner of a touched file, trial-merged against its tip. hierarchies-unit adds its own read-only readers to `scripts/hierarchies/measure.py`; this phase does not edit it. roles-unit writes the duties; this phase changes no contract text.

**Acceptance gate:** the gates in Delegation Context green, `basedpyright` on each slot's files; a live scratch run records a wait of each kind that has an emit point and reads it back; a scratch merge writes a receipt and the read verb names a merge without one `ReceiptMissing`; the checkpoint notice names the file and commit for hierarchies-unit.
