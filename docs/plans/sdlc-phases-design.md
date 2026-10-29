# Phase process and document pipeline

This document maps the current process. It records its cost and its known problems.
It does not approve a change to that process.

Source plan: [sdlc-phases.md](sdlc-phases.md), Decisions and Phase 1.
Repair rounds, phase meaning in run records, and fix time per phase belong to
fix-rounds-unit. The baseline now reads its `scripts/history/rounds.py`.
Fix costs appear as worker time apart from phase wall-time shares.
This scope comes from the sdlc showrunner through the unit director, 2026-10-20.
Who designs and who reviews belongs to guilds-unit. Responsibilities and names
belong to roles-unit. A proposal that reaches those areas goes through the showrunner.
Every process change needs the user's approval before a live process file changes.

## Map

The phase table follows `commands/unit/direct.md`, `<ExecutionSteps/>`, and
`docs/delegate/phase_end.md`, `<PhaseEnd/>`. It describes loop and verbose mode.
Conditional steps say when they run. A tag in this table is a literal contract
name, not a new step or a proposed rule.

The map shows the current phase contract. The baseline uses the phase start and
finish events that exist in the run records. One run record can hold more than
one plan phase, so a recorded interval is not proof of one completed plan phase.
The measure uses phase meaning and fix costs from `scripts/history/rounds.py`.
It keeps no second store of those facts. It excludes mixed phases from per-phase
costs and reports open phases apart. In this window, mixed n=31 and open
unmixed n=81. Do not compare the changed counts with Phase 1 as a saving.
Source: Baseline, exclusion note; fix-rounds-unit via the sdlc showrunner, relayed
by the unit director, 2026-10-20.

The recorder opens a phase just before dispatch. Work Order compose and its gates
come before that boundary. For a later phase, their elapsed time can appear in
the gap from the prior `phase_finished` to the next `phase_started`. The first
phase has no prior phase gap. The doc pipeline records run start to first phase
start. The director-token table can allocate only turns that its source maps to
a phase. These boundaries prevent a separate claim about compose time when the
source has no such interval.

In the baseline column, `pass: implementation`, `pass: test`, and `pass: review`
mean rows mapped by `scripts/history/history.py`, `PASS_STAGE`. The fix row names
fix-rounds-unit's source and reports worker time apart. Activity names pass
through that file's `_clean_label`; aliases such as `fold` become `plan update`.
An activity exists only when it was recorded. `director time` is the time inside
the phase that no recorded pass or activity covers. It is not proof that the
director worked for the whole interval. When distinct labels overlap, the measure
shares their elapsed seconds equally. Parallel workers with the same label count
once. These are allocated wall-time costs; they are not each worker's full time.

| Step, in order | Defining file and tag | Runs it | Reads | Writes | Baseline row or boundary |
| --- | --- | --- | --- | --- | --- |
| 1. Compose the Work Order | `docs/delegate/compose_work_order.md`, `<ComposeWorkOrder/>`; `docs/delegate/write_prompt_contract.md`, `<WritePromptContract/>` | Director; Work Order validator and recorder | Target Work Order, Delegation Context, prior constraints, command amendments, Pending decisions; fallback reads enough code to fill a missing plan | Validated phase prompt, resolved decision edits, run record, next-items path | `Phase end through next Work Order → phase end to next phase start`; for the first phase, `Doc pipeline summary → run to first phase`. `Director tokens summary → before first pass share` is a proxy inside the phase; compose before start is not isolated |
| 2. Brief and obtain the required authority | `commands/unit/brief.md`, `<VerbosePrePhaseGate/>`, `<BriefingFreshness/>`, `<AutoWindowBatchBriefing/>`; `commands/unit/direct.md`, `<AuthorizationContract/>` | Director; user when a gate applies | Current Work Orders, allowed mode and auto window, pending add-ons | Briefing and accepted window; decisions in their Work Orders | `Phase end through next Work Order → phase end to next phase start`; first-phase case: `Doc pipeline summary → run to first phase`. Separate briefing cost: `not recorded`; source would be a briefing activity in `scripts/direct/progress_history.py` |
| 3. Check production gates and merge the merge branch | `docs/production_format.md`, `<ProductionUnit/>` items 5–6 | Director; git | Production doc, gate checkpoint ancestry, origin merge branch | Updated unit tree; conflict fixes and their verification when needed | `Phase end through next Work Order → phase end to next phase start`. Separate gate and merge cost: `not recorded`; source would be gate and merge activities in `scripts/direct/progress_history.py` |
| 4. Coordinate the phase reservation | `commands/unit/direct.md`, `<CoordinateDelegatedPhaseReservation/>`, `<DelegatedPhaseReservationContract/>` | Director; coordination scripts | Validated Work Order paths, repository enrollment, durable phase reservation state | Phase reservation state or a reported refusal; exact paths are claimed on first edit | `Phase end through next Work Order → phase end to next phase start`; first-phase case: `Doc pipeline summary → run to first phase`. Separate reservation cost: `not recorded`; source would be a reservation activity in `scripts/direct/progress_history.py` |
| 5. Save the baseline and open the phase | `docs/delegate/launch_implementation.md`, `<LaunchImplementation/>` steps 1–2; `commands/unit/direct.md`, `<PassOwnership/>` | Director; recorder | Git HEAD and status, original phase prompt, outgoing phase state | `phase_baseline`, `progress_baseline_status`, `phase_started` record | `Phase steps → phase wall` begins at `phase_started`; `Doc pipeline summary → run to first phase` ends there |
| 6. Assign and dispatch both slots | `docs/delegate/launch_implementation.md`, `<LaunchImplementation/>`; `docs/delegate/write_prompt_contract.md`, `<PhaseTeam/>`, `<TeamFilePartition/>`, `<RoleReassignment/>`, `<PhaseMesh/>`, `<CoordinationBoard/>` | Director; launchers | Workers field, disjoint file sets, open workers, agent config, complete prompts | Slot prompts, role posts, launcher-owned pass records | `Phase steps → pass: implementation` and `pass: test`, by assigned kind; a second writer records `pass: implementation` |
| 7. Write the change and its tests; run listed checks | `docs/delegate/write_prompt_contract.md`, `<WritePromptContract/>`; `commands/unit/direct.md`, `<VerificationContract/>`, `<WorkerRuns/>`; `docs/delegate/phase_checks.md`, `<PhaseChecks/>` | Workers; verification scripts | Specification, owned files, named code and exact verification lines | Code, tests, check results, slot summaries, terminal worker status | `Phase steps → pass: implementation`, `pass: test`, and `activity: Verification`; a recorded check can overlap its pass |
| 8. Arm an early reviewer when the trigger applies | `commands/unit/direct.md`, `<EarlyReviewArm/>`; `docs/delegate/dual_review.md`, `<BroadReviewPrompt/>` early form | Director; reviewer; launcher | Partial diff, specification, progress trigger; later final diff | Provisional read, then final review findings | `Phase steps → pass: review`; it can overlap `pass: implementation`. A separate early-review row is `not recorded`; source would be a labeled review interval in `scripts/direct/progress_history.py` |
| 9. Review the full change and style | `docs/delegate/dual_review.md`, `<DualReview/>`, `<TeamReview/>`, `<ReviewPromptContract/>`; `docs/delegate/phase_checks.md`, `<PhaseChecks/>`; `commands/unit/style_review.md`, `<StyleReview/>` | Two review lenses; director for main review and style | Complete diff from saved baseline, untracked files, specification, affected callers; director alone reads slot summaries | Lens findings, main findings, style report, ledger entries for style | `Phase steps → pass: review` and uncovered `director time`. Separate style cost: `not recorded` in this summary; source would be a style activity in `scripts/direct/progress_history.py` |
| 10. Run application smoke and collect design checks where applicable | `commands/unit/direct.md`, `<RunApplicationSmokeTest/>`, `<UXReview/>`, `<ExecutionSteps/>` step 6 | Director; app; design reviewer | Changed runtime paths, real product, UX guide, shots of every changed state | Action and result, screenshots, design verdict, defect findings or exact deferred action | `Phase steps → activity: smoke`, `activity: Mac smoke`, and `activity: UX review`; uncovered work falls in `director time` |
| 11. Combine findings and choose the ledger's next action | `commands/unit/direct.md`, `<Synthesize/>`, `<FindingsLedger/>`, `<DelegationResultFormat/>`, `<DecisionRouting/>` | Director; findings script | All landed reviews, smoke and design findings, existing finding ids, applicable decision rules | One deduplicated set, result report, ledger entries and gate verdict; direct trivial fixes with evidence | `Phase steps → director time` for uncovered work. Separate synthesis cost: `not recorded` in this summary; source would be a synthesis activity in `scripts/direct/progress_history.py` |
| 12. Repair and check closure when the ledger requires it | `commands/unit/direct.md`, `<FixDispatch/>`; `docs/delegate/dual_review.md`, `<ClosureReview/>`; `docs/production_format.md`, `<ProductionUnit/>` item 7 | Repair worker; closure reviewer; director; scripts | Complete batch of ids, affected paths and callers, repair diff, gate result | Repairs, required check results, closure verdicts, updated ledger | `Fix costs → fix worker time` and `repair rounds`, from `scripts/history/rounds.py`; closure pass: `Phase steps → pass: review`. Worker time can exceed wall time; merge-caused repairs stay under fix |
| 13. Confirm smoke and the design verdict after relevant repairs | `commands/unit/direct.md`, `<ExecutionSteps/>` step 8, `<RunApplicationSmokeTest/>`, `<UXReview/>` steps 5–6 | Director; design reviewer | Current product and current shots, prior findings; checkpoint notice needs a fresh full-set verdict | New smoke result and design verdict, or further findings | `Phase steps → activity: smoke` and `activity: UX review`; uncovered work falls in `director time` |
| 14. Commit the code and confirm reservation release | `docs/delegate/phase_end.md`, `<PhaseEnd/>` step 1; `commands/unit/checkpoint.md`, `<CheckpointCommit/>`, `<PushCheckpoint/>` | Director; validation, ledger, git and reservation scripts | Current checks, phase baseline, exact durable reservation record, finished Work Order, owned changed paths | Code checkpoint with phase `done` and Work Order intact, confirmed release, push result, review-trial and parallel lines | `Director tokens summary → after last pass share`; uncovered work: `Phase steps → director time`. Separate checkpoint cost: `not recorded`; source would be a checkpoint activity in `scripts/direct/progress_history.py` |
| 15. Send the checkpoint notice | `docs/delegate/phase_end.md`, `<PhaseEnd/>` step 2; `docs/production_format.md`, `<ProductionUnit/>` item 3 | Director; recorder | Mergeable hash, next phase and recorded ETA, review-trial and parallel output, fresh design result | Checkpoint notice, next-phase ETA record | `Phase steps → director time` for uncovered work. Separate notice cost: `not recorded`; source would be a notice activity in `scripts/direct/progress_history.py`. Notice time is not merge time |
| 16. Review what the shipped phase changes ahead | `docs/delegate/run_phase_review.md`, `<RunPhaseReview/>`; `commands/plan/phase_review.md`, steps 3–5 and `<StateAndConsequenceAudit/>`, `<MaintainWorkOrders/>` | Director; architect reviewer only on a listed trigger | Shipped phase, remaining Work Orders and next items, ledger advisory, temporary closeout facts | Temporary retrospective and review output; corrected `todo` Work Orders and Pending decisions; post-checkpoint defects get a follow-up phase | `Phase steps → pass: review` for an architect pass; uncovered work: `director time`. `Plan churn summary → Work Order edits`, `decisions opened`, and `decisions closed` record committed changes. Separate plan-review time: `not recorded` in this summary; source would be its activity in `scripts/direct/progress_history.py` |
| 17. Check add-ons | `commands/unit/add_ons.md`, `<ConsiderNextItems/>`, `<ReviewPendingAddOns/>`; `docs/delegate/phase_end.md`, step 4 | Director; showrunner in a production; user at an interactive point outside a production | Remaining plan, discovered work, next-items file, prior approved dispositions | In-scope corrections; temporary pending add-ons; approved `current` work or `next` items only | `Phase steps → director time` for uncovered work; `Plan churn summary → Work Order edits` for committed plan changes. Separate add-on cost: `not recorded`; source would be an add-on activity in `scripts/direct/progress_history.py`; later decisions can fall outside this phase |
| 18. Clean up completed work and close the phase | `commands/unit/direct.md`, `<PhaseCleanup/>`, `<RecordPhaseCompletion/>`; `docs/delegate/phase_end.md`, step 5 | Director; cleanup and recorder scripts | Finished helpers and apps, reservation disposition, phase state | Worker cleanup without ending Codex threads; `phase_finished`; inactive reservation-state removal | `Phase steps → phase wall` ends at `phase_finished`. Separate cleanup cost: `not recorded`; source would be a cleanup activity in `scripts/direct/progress_history.py` |
| 19. Choose the next phase; report and gate when required | `commands/unit/direct.md`, `<NextPhase/>`, `<VerbosePostPhaseGate/>`; `commands/unit/phase_report.md`, `<VerbosePostPhaseReport/>`, `<RemainingWorkOutlook/>`; `commands/unit/ci.md`, `<PeriodicCI/>` | Director; CI scripts outside a production; showrunner owns production CI | Dependencies, remaining Work Orders and decisions, mode, auto window, checkpoint count | Next selection; outside an auto window, shrink before report and gate; at a due CI point, shrink before CI | `Phase end through next Work Order → phase end to next phase start`. Separate selection, report, and CI costs: `not recorded`; source would be labeled activities in `scripts/direct/progress_history.py`; a user stop ends the chain |
| 20. Compose and start the next phase | `commands/unit/direct.md`, `<NextPhase/>`; `docs/delegate/compose_work_order.md`, `<ComposeWorkOrder/>`; `docs/delegate/launch_implementation.md`, step 2 | Director; validator and recorder | Reviewed next Work Order, its gates, next phase baseline; repeat steps 1–6 | Next prompt, next `phase_started`, slot dispatches | `Phase end through next Work Order → phase end to next phase start` ends at the next `phase_started`; `Phase steps → phase wall` starts there |
| 21. Shrink the completed phase, normally beside the next workers | `docs/delegate/phase_end.md`, step 7; `docs/delegate/run_phase_review.md`, `<RunPhaseShrink/>`; `commands/plan/shrink.md`, `<ArchiveOnly/>`, `<Verify/>`; `commands/unit/checkpoint.md`, `<ShrinkCommit/>` | Director; shrink helpers, validator and git | Completed Work Order, temporary closeout facts, remaining dependencies, approved next items | Completed phase's `As-built` block, structural result, shrink commit and notice, cleared review prose | `Phase steps → activity: shrink`; after the next start it can count against the next phase; outside a phase it is not phase wall time |
| 22. Close a completed run instead of starting another phase | `commands/unit/direct.md`, `<FinalGate/>`, `<RunSummary/>`; `docs/delegate/final_gate_commit.md`, `<FinalGateCommit/>`, `<RunAsBuilt/>`, `<AsBuiltCommit/>` | Director; verification scripts; doc helpers | Final tree, all completed phases, deferred smoke, issues and doc links | Final checks and repairs if needed, as-built docs and commit, issue closures, run summary and `run_finished` | `not recorded` as a separate run-closeout cost; source would be `run_finished` and labeled closeout activities in `scripts/direct/progress_history.py`. A recorded synthetic final phase can enter `Phase steps → phase wall` |

Steps 19–21 have a branch in their order. Loop mode launches the next workers
before shrink. A due CI point runs step 21 and CI before step 20.
Verbose mode outside an auto window runs shrink, report, and gate before the next
launch. The table shows both boundaries because shrink can cross the next start.
Review checks require a stable worktree; a shrink write must not run during a
code review that checks the same worktree.

`single` runs steps 1–13, then plan review, shrink, add-on check, both cleanup
halves, and phase completion. It makes no checkpoint. A complete `single` task
runs the as-built pass without a commit. A standing plan skips shrink and run
closeout; `<StandingPlanRefill/>` in `commands/unit/direct.md` supplies its next
ticket.

The document table is a map of possible inputs and outputs. It is not a rule to
make every kind for every change. As-built is the output after implementation;
an existing as-built can be an input before a later plan.

| Document kind | Defining file or tag | Command that writes it | Command that reads it | Reads and writes | Format file | Baseline link |
| --- | --- | --- | --- | --- | --- | --- |
| Proposal issue | `commands/showrunner/proposal.md`, File / Approve / Start and close | `/issue` files a finished proposal; `/showrunner:proposal` routes its approval and lifecycle through `proposal_issue.py` | `/showrunner:proposals`, `/next proposal`; `/unit:direct` reads linked issues and the approved plan | Finished proposal → issue with `proposal`, author and proposed ranking; approval → plan and unit link; complete run → closure with as-built | `~/rust/hanadocs/.claude/templates/issue.yaml`; proposal fields and exceptions in `commands/showrunner/proposal.md` | `Doc pipeline summary → first doc to first phase` can include a named source with a git commit. Separate issue creation and approval costs: `not recorded`; sources would be issue lifecycle records and source-repository git history |
| Review output | `commands/team_review.md`, `<EstablishWorkingDoc/>`; `commands/api_review.md`, `<WriteDoc/>`; `commands/module_review.md`, `<WritePlacementPlan/>`; `commands/adversarial.md`, Frame / Report; `commands/astra_review.md`, Steps; `commands/adhoc_review.md`, steps 2 and 4 | `/team_review` updates its working doc; `/api_review` and `/module_review` write review plans; `/adversarial` saves scratch evidence; `/astra_review` saves a review; `/adhoc_review` records decisions | `/plan:to_phased_plan` reads a resolved review plan; a calling design workflow folds in advice; `/adhoc_review` reads open choices | Scope, code or design and evidence → findings, recommendations, accepted decisions or review plan; scratch reviews need not become a plan | Each named command defines its own output; no shared review-doc format file | `Doc pipeline summary → first doc to first phase` can include a named review doc with a git commit. Separate review-command cost: `not recorded`; sources would be the retained review header and command records. Scratch steps without these remain unknown |
| Design doc | `commands/plan/to_phased_plan.md`, Purpose / `<Locate/>`; this source plan, Phase 1 | The task's author writes it; no common design-doc command is defined by these sources | Review commands; `/plan:to_phased_plan`; later `/plan:to_as_built` can read its disposition for diff mode | Goals, evidence and decisions → resolved design; compile normally rewrites it in place; `--out` keeps it | No shared design-doc format file in the named contracts; this doc follows `docs/plans/sdlc-phases.md`, Phase 1 | `Doc pipeline summary → first doc to first phase` can include a named design source with a git commit. Separate design-writing cost: `not recorded`; source would be design creation and completion records; git does not record uncommitted work |
| Phased plan | `commands/plan/to_phased_plan.md`, `<GatherContext/>`, `<Rewrite/>`, `<ValidateWorkOrders/>`, `<PhaseNumbering/>` | `/plan:to_phased_plan`; `/plan:phase_review` updates remaining Work Orders; `/plan:shrink` archives completed ones | `/unit:direct`, `/producer:greenlight`, `/producer:to_production`, `/plan:phase_review`, `/plan:shrink`, `/plan:to_as_built` | Resolved design or review plan plus researched code context → Delegation Context and validated Work Orders; shared validation reads Goal/Spec/Files | `docs/delegate_plan_format.md` | `Doc pipeline summary → first doc to run` and `run to first phase`; `Plan churn summary → Work Order edits`, `decisions opened`, `decisions closed`, `phases inserted`, and `named phase splits` |
| Production doc | `commands/producer/greenlight.md`, `<Report/>`; `commands/producer/to_production.md`, `<WriteProductionDoc/>` | `/producer:to_production`; greenlight first returns a read-only report | `/showrunner:produce`; `/unit:direct` through its plan's Production line | Delegate-ready plans and greenlight units, hubs, gates and capacity → production context, Units, Gates, rules and closeout | `docs/production_format.md` | `Doc pipeline summary → first doc to first phase` can include a named production source with a git commit. Separate greenlight and production-doc costs: `not recorded`; sources would be command records linked to the production doc |
| Unit plan | `commands/producer/to_production.md`, `<WriteUnitPlans/>`, `<Validate/>`; `docs/production_format.md`, `<ProductionUnit/>` | `/producer:to_production` keeps one source plan and writes others; `/plan:phase_review` and `/plan:shrink` maintain each run's plan | `/showrunner:produce`; `/unit:direct` and phase helpers | Source Work Orders → per-unit Work Orders, narrowed context, Production line, dependencies and gates; moved Spec stays verbatim | `docs/delegate_plan_format.md`; production header and rules in `docs/production_format.md` | `Doc pipeline summary → first doc to run` and `run to first phase`; `Plan churn summary → Work Order edits` and `phases inserted`, keyed to the unit plan path |
| As-built | `commands/plan/to_as_built.md`, `<Verify/>`, `<Distill/>`, `<Relocate/>`, `<ReconcileAsBuilt/>`; `docs/delegate/final_gate_commit.md`, `<RunAsBuilt/>` | `/plan:to_as_built` from a completed plan or `--from-diff`; director commits in loop/verbose | Later planning and review commands read current design and invariants; later as-built passes check peers for contradictions | Shipped code and plan facts → current feature overview or amendments to existing docs; deletes the replaced plan and fixes links; closes linked issues at run end | `commands/plan/to_as_built.md`, `<Distill/>`; no separate shared format file; per-phase `As-built` blocks use `docs/delegate_plan_format.md` | `not recorded` as a separate closeout cost in this baseline; sources would be the as-built commit in git and its activity records in `scripts/direct/progress_history.py`. Git can retain the old plan path |

Contract load is the static size of files named by full-file read calls in
`commands/unit/direct.md` and its contracts. The minimum per-phase set has
13 files, 4252 lines, and 239441 bytes. It counts each per-phase and dispatch
file once; more dispatches add reads. The full set has 16 files. Briefing and
report reads depend on the request or mode. `final_gate_commit.md` is a run-end
read; an ad hoc task skips plan review and shrink. These sizes describe required
reads. They do not prove which files a past director read or how many tokens
those reads used. Source: Baseline, `Contract full reads → sum per phase (minimum)`
and `Contract size summary → lines per file` / `bytes per file`.

## Baseline

Scope: `scripts/history/rounds.py` now supplies phase boundaries and fix worker
time. Open and mixed phases are counted apart. A closure review stays in the
review row; a ledger-only repair round adds a round but no fix worker seconds.
Repair coverage stays under fix even for a finish-only pass record. Merge
activity outside that repair interval remains merge coverage.
A late document has a separate detail state. A plan is `doc after dispatch`
only when all named first commits are recorded and the earliest is after
dispatch. Exclude that negative-lead plan from lead-time costs and rankings.
A late additional document does not exclude a valid earlier lead. A missing
named commit makes the lead `not recorded`.
These measurement changes replace Phase 1's counts; they are not process savings.

Command: `python3 scripts/direct/phase_process.py baseline`.
Window: `[2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)`.
The real-data run on 2026-10-20 at 18:34 AST finished with exit 0.
`--detail <table>` prints full rows; the saved default output below shows summaries
and top lists. The block contains the complete stdout from this run.
Source: `/tmp/claude/unit/4b421ed5-d048-4689-bd8d-adf04d343705/mesh_runs/20261020T183407-3260254-378b.log`.

```text
Phase process baseline — natedev only; no Mac records read. Use baseline --detail TABLE for full rows.
n counts phases, plans or files as labeled; p90 is nearest rank. Top lists show 10 plus summed other.
Distinct overlapping steps share wall time equally; parallel workers of one kind count once. Repair wall coverage is excluded from shares; fix costs are worker time, shown apart.
Director time includes uncovered waits; unmatched steps have unknown coverage. Activity tails below 0.1% are merged.
rounds.py owns phase boundaries; mixed phases are excluded. Plans are selected by run start, completed phases by finish.
Token input includes caches; pass records bound before/after shares of input + output. Shared sessions can overlap; compose before phase start is not isolated.

Phase steps — natedev only | [2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)
step                         n phases  median   p90      total     wall share
director time                1427      306.2s   1.21h    1203.13h  38.4%
pass: implementation         1343      438.3s   1.05h    716.53h   22.9%
pass: test                   490       403.3s   1.13h    215.89h   6.9%
activity: Verification       215       609.3s   1.60h    138.73h   4.4%
pass: review                 623       296.1s   1081.6s  79.86h    2.6%
activity: shrink             285       79.1s    226.9s   23.03h    0.7%
activity: UX review          95        360.4s   1588.5s  17.28h    0.6%
activity: Mac smoke          1         11.77h   11.77h   11.77h    0.4%
activity: other (38 labels)  59        186.6s   1601.9s  9.53h     0.3%
activity: smoke              26        53.4s    3361.3s  4.84h     0.2%
activity: live check         13        247.8s   3216.8s  3.25h     0.1%
phase wall                   1427      1353.5s  4.11h    3131.06h  100%

Fix costs — worker time, natedev only; source: history.py rounds | [2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)
measure          n phases  median  p90    total    not recorded
fix worker time  1427      0.0s    1.11h  923.97h  0
repair rounds    1427      0       2      1,151    0
Excluded from per-phase costs: mixed n=31 (finished in window or open); open unmixed n=81 (started before window end).

Phase end through next Work Order — natedev only | [2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)
step                           n phases  median  p90      total
phase end to next phase start  1291      103.3s  2767.1s  1261.10h

Director tokens summary — natedev only | [2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)
measure                  n phases  median       p90         total           not recorded
input per phase          1424      5,904,804.5  39,850,800  25,408,071,192  3
output per phase         1424      14,457.5     149,936     85,329,253      3
before first pass share  1387      11.6%        44.0%       —               40
after last pass share    1387      40.5%        74.8%       —               40

Director tokens: top 10 phases by input — natedev only | [2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)
run/phase@start                       n phases  input           output      n turns
f0ad402c/17@2026-10-07 07:05:39 AST   1         350,978,965     1,354,683   1,728
f80f42eb/20@2026-10-06 17:46:48 AST   1         323,843,858     1,187,991   1,659
6df1f9fc/66@2026-10-07 05:01:19 AST   1         322,980,520     1,252,341   1,586
720c2a10/120@2026-10-15 10:44:59 AST  1         314,060,185     831,357     1,662
a702c130/15@2026-10-06 22:49:59 AST   1         311,014,536     995,869     1,606
24e20a41/17@2026-10-17 08:25:51 AST   1         299,101,922     1,226,040   2,247
9a76bbd6/52g@2026-10-14 16:33:37 AST  1         280,390,322     744,322     1,501
61cc30d4/63@2026-10-06 11:14:49 AST   1         264,160,282     858,619     1,347
1a1f39e6/6@2026-10-11 05:22:09 AST    1         252,648,768     938,994     1,403
f93f565a/31@2026-10-15 07:53:20 AST   1         244,122,257     645,047     1,287
other (n=1414)                        1414      22,444,769,577  75,293,990  121,025
not recorded (usage.db turns (collect.py): no mapped turns in phase); n phases=3

Contract size summary — static checkout | [2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)
measure         n files  median   p90     total    not recorded
lines per file  16       134      540     4,675    0
bytes per file  16       6,103.5  33,476  258,411  0

Contract full reads — static checkout | [2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)
file                                    n files  lines  bytes   cadence
commands/unit/direct.md                 1        1579   92400   per phase
commands/plan/phase_review.md           1        540    33476   per phase
commands/plan/shrink.md                 1        524    24125   per phase
docs/delegate/write_prompt_contract.md  1        384    23224   per dispatch
commands/unit/checkpoint.md             1        310    17964   per phase
docs/delegate/dual_review.md            1        318    16188   per dispatch
commands/unit/brief.md                  1        171    7992    on request
commands/unit/add_ons.md                1        126    6268    per phase
commands/unit/phase_report.md           1        142    5939    on request
docs/delegate/launch_implementation.md  1        92     5678    per phase
other (n=6)                             6        489    25157   final gate only: 1; per phase: 5
sum per phase (minimum)                 13       4252   239441  includes one read per dispatch file
Per-phase sum counts each per-phase/dispatch file once; repeated dispatches add reads. Brief/report reads are on request or mode-dependent.

Doc pipeline summary — natedev only | [2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)
measure                   n plans  median  p90     total     not recorded
first doc to run          33       594.5s  47.43h  1264.85h  54
run to first phase        87       84.3s   181.8s  2.79h     0
first doc to first phase  33       751.3s  47.45h  1265.95h  54
doc after dispatch: n plans=11; n documents=19; negative-lead plans excluded from all lead distributions, totals and ranking; see --detail doc-pipeline.

Doc pipeline: top 10 plans by lead time — natedev only | [2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)
plan                                      n plans  doc to phase  doc to run  run to phase
tool-based-ui-startup-polish.md           1        710.20h       710.20h     2.0s
tool-based-ui.md                          1        332.80h       332.78h     86.2s
info-panels-plan.md                       1        97.21h        97.21h      6.8s
tool-based-ui-inspectors.md               1        47.45h        47.43h      86.5s
rich-text-plan.md                         1        38.27h        38.10h      595.7s
hana-organon-design.md                    1        27.35h        27.28h      234.4s
build-followups-herdr-panes.md            1        4.06h         4.04h       77.3s
build-followups-efficiency.md             1        3.41h         3.39h       74.7s
build-followups-review-pool-followups.md  1        1.56h         1.55h       57.4s
remove-running-section-tile-fixes.md      1        1008.9s       930.5s      78.4s
other (n=77)                              77       3.36h         2.62h       2.43h
Earliest doc requires all named source commits. Document commits not recorded: n=55/119 documents; source: git log. Commands and exact milestones: --detail doc-pipeline.

Plan churn summary — natedev only; first run through window end | [2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)
measure             n plans  median  p90  total  not recorded
Work Order edits    36       1       4    58     62
decisions opened    36       0       1    10     62
decisions closed    36       0       1    11     62
phases inserted     36       5       53   539    62
named phase splits  2        4.5     7    9      96

Plan churn: top 10 plans by total changes — natedev only | [2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)
plan                                  n plans  total  todo  opened  closed  inserted  splits
tool-based-ui.md                      1        91     1     3       2       85        not recorded
build-followups-two-accounts.md       1        85     17    0       0       68        not recorded
hana-organon-design.md                1        78     0     0       0       78        not recorded
widget-enhancements-plan.md           1        67     3     3       3       51        7
frame-time-plan.md                    1        63     8     1       1       53        not recorded
remove-running-section-tile-fixes.md  1        33     2     3       3       23        2
build-followups-style.md              1        26     4     0       0       22        not recorded
tool-based-ui-cables.md               1        25     2     0       0       23        not recorded
info-panels-plan.md                   1        20     2     0       0       18        not recorded
build-followups-unit-board.md         1        20     0     0       0       20        not recorded
other (n=26)                          26       119    19    0       2       98        not recorded
Split counts use named git messages; absence is not recorded. Other totals sum known values only; unknown counts are above.
Churn counts committed snapshots from first run, possibly before this window. Removed pending blocks count as closed.
Git times are commit times; command names are evidence, not proof of execution. Uncommitted churn is not recorded.
Source read problems: n=0; first 3: none
```

## Friction

`M/` below means `~/.claude/projects/-home-natepiano--claude/memory/`.
`reported` means a source states the problem; it is not a new measured cost.
A reported problem can already have a guard in the current contract.
`measured` uses the baseline window
`[2026-10-06 18:34:07 AST, 2026-10-20 18:34:07 AST)` and natedev records.
The time lines come first, from largest to smallest total. They use different
samples and can overlap; do not add their totals. Token, read-load, and churn
lines follow because their units differ. The current checkout supplies read
sizes. Doc lead time can start before the window; churn starts at the first run.

- **measured** — First doc to first phase totals 1265.95 h over 33 plans; median 751.3 s, p90 47.45 h. The largest listed plan takes 710.20 h (n=1). These spans include time before the window and can include idle time. Lead time is not recorded for 54 plans. First commits are not recorded for 55 of 119 documents. Negative earliest leads exclude 11 plans from lead-time distributions, totals and rankings. Separately, 19 documents have a first commit after dispatch; a later additional document does not exclude a valid earlier lead. Missing source commits make the lead not recorded. Sources: Baseline, `Doc pipeline summary → first doc to first phase`; `Doc pipeline: top 10 plans by lead time → tool-based-ui-startup-polish.md`; document-commit coverage and `doc after dispatch` notes.
- **measured** — Phase end to next phase start totals 1261.10 h over 1291 gaps; median 103.3 s, p90 2767.1 s. It can include next Work Order compose, gates, and waits; it is not all closeout work. Source: Baseline, `Phase end through next Work Order → phase end to next phase start`.
- **measured** — Uncovered director time takes 38.4% of phase wall time: 1203.13 h over 1427 completed non-mixed phases; median 306.2 s, p90 1.21 h. It includes waits. It is an upper bound on director work in these uncovered intervals, not a measure of active work across the whole phase. Source: Baseline, `Phase steps → director time`.
- **measured** — Implementation takes 22.9% of phase wall time: 716.53 h over 1343 completed non-mixed phases; median 438.3 s, p90 1.05 h. This is the largest measured worker step. Source: Baseline, `Phase steps → pass: implementation`.
- **measured** — Test passes take 6.9% of phase wall time: 215.89 h over 490 completed non-mixed phases; median 403.3 s, p90 1.13 h. Source: Baseline, `Phase steps → pass: test`.
- **measured** — Verification takes 4.4% of phase wall time: 138.73 h over 215 completed non-mixed phases; median 609.3 s, p90 1.60 h. The overlap rule shares elapsed time with a worker pass when both cover it. Source: Baseline, `Phase steps → activity: Verification`.
- **measured** — Review passes take 2.6% of phase wall time: 79.86 h over 623 completed non-mixed phases; median 296.1 s, p90 1081.6 s. Director review without a pass or activity record can fall in uncovered time. Source: Baseline, `Phase steps → pass: review`.
- **measured** — Shrink takes 23.03 h over 285 completed non-mixed phases; median 79.1 s, p90 226.9 s, 0.7% of phase wall time. Shrink outside a recorded phase is not in this row. Source: Baseline, `Phase steps → activity: shrink`.
- **measured** — UX review takes 17.28 h over 95 completed non-mixed phases; median 360.4 s, p90 1588.5 s, 0.6% of phase wall time. Source: Baseline, `Phase steps → activity: UX review`.
- **measured** — One Mac smoke activity takes 11.77 h (n=1; median and p90 11.77 h), 0.4% of phase wall time. It is in natedev's records; the script reads no Mac records. Source: Baseline, `Phase steps → activity: Mac smoke`.
- **measured** — The 38 small activity labels together take 9.53 h over 59 completed non-mixed phases; median 186.6 s, p90 1601.9 s, 0.3% of phase wall time. This summary cannot assign their cost to each named step. Source: Baseline, `Phase steps → activity: other (38 labels)`.
- **measured** — Smoke takes 4.84 h over 26 completed non-mixed phases; median 53.4 s, p90 3361.3 s, 0.2% of phase wall time. Source: Baseline, `Phase steps → activity: smoke`.
- **measured** — Live checks take 3.25 h over 13 completed non-mixed phases; median 247.8 s, p90 3216.8 s, 0.1% of phase wall time. Source: Baseline, `Phase steps → activity: live check`.
- **measured** — Director input totals 25,408,071,192 tokens over 1424 completed non-mixed phases; median 5,904,804.5 (about 5.9 M), p90 39,850,800 (about 39.9 M). Input includes caches, and shared sessions can overlap. Over 1387 phases with pass bounds, median input-plus-output share is 40.5% after the last pass and 11.6% before the first pass. These shares do not isolate compose before phase start. Sources: Baseline, `Director tokens summary → input per phase`, `after last pass share`, and `before first pass share`.
- **measured** — The contract requires at least 4252 lines, 239441 bytes (about 239 KB), across 13 files per phase, with one read of each dispatch file. `direct.md` alone has 1579 lines and 92400 bytes (n=1 file). More dispatches add reads. This is static required load, not proof of past reads. Sources: Baseline, `Contract full reads → sum per phase (minimum)` and `commands/unit/direct.md`.
- **measured** — Phases inserted after run start total 539 over 36 plans; median 5 per plan, p90 53. Churn follows each plan from its first run through the window end, so some edits can precede the window. Sources: Baseline, `Plan churn summary → phases inserted`; churn-window note.

- **reported** — One run record can hold more than one plan phase. fix-rounds-unit owns phase meaning and fix cost per plan phase in `scripts/history/rounds.py`; the refreshed Baseline now excludes mixed phases. Source: fix-rounds-unit via the sdlc showrunner, relayed by the unit director, 2026-10-20; Baseline, exclusion note.
- **reported** — A gate that checked only changed folders missed shared callers and tree-wide checks; later merge gates failed. The current root merge gate covers them. Source: `M/shared-module-full-gate.md`, `M/run-lib-suite-in-gates.md`.
- **reported** — Cleanup of old top-level review prompts also deleted a later phase's prompts. Phase-local prompts now avoid that collision; old top-level callers still need care. Source: `M/clear-phase-review-wipes-all-prompts.md`; `commands/unit/direct.md`, `PHASE_DIR` and `<PhaseCleanup/>`.
- **reported** — A handoff or shrink write during a review made that review fail even when its reviewed code stayed the same. The current shrink overlap still needs a stable interval before the next review. Source: `M/no-worktree-writes-during-review.md`; `docs/delegate/phase_end.md`, step 7; `docs/delegate/dual_review.md`, `<ReviewPromptContract/>`.
- **reported** — Direct one-edit fixes once bypassed the findings ledger, so review-trial totals missed real findings. Every confirmed finding must keep its id and closure evidence. Source: `M/record-direct-review-fixes.md`; `commands/unit/direct.md`, `<FindingsLedger/>`, `<Synthesize/>`.
- **reported** — A request to correct stale as-built text used a user turn for a correction the user already allowed. The current rule allows corrections across unit ownership; a process redesign still needs the separate approval in this plan. Source: `M/correct-stale-as-builts-freely.md`; `docs/production_format.md`, `<ProductionUnit/>` item 9; `docs/plans/sdlc-phases.md`, Decisions.
- **reported** — Scratch recorder checks failed when identity flags were missing or attached to the wrong subcommand. Only `start-run` takes those identity flags. Source: `M/attempts-recorder-live-check.md`.
- **reported** — A timed-out worker compaction left a live turn that refused follow-up work; a repeated launch did not clear it. The log records lost tester time, but it is not a cost measured by this baseline. Its old replacement advice must be checked against current worker-persistence rules before use. Source: `M/attempts-worker-follow-refused-after-compaction.md`; `M/workers-persist-across-phases.md`; `commands/unit/direct.md`, `<LongLivedWorkers/>`.
- **reported** — A background Claude helper started another background run and did not receive its result for hours. This report concerns nested Claude task delivery; Codex worker commands use their own result-delivery path. Source: `M/subagent-foreground-suites.md`; `commands/unit/direct.md`, `<WorkerRuns/>`.
- **reported** — Polls, sleeps and repeated status calls consumed worker context while no work advanced. The current prompt contract forbids those calls. Source: `M/model-study-guards-usage.md`; `docs/delegate/write_prompt_contract.md`, `<WritePromptContract/>` item 3.
- **reported** — A proposal to use a cheaper director model based on speed and token counts missed judgment quality. Process savings must keep the user's judgment requirement. Model assignments remain outside this unit's step changes. Source: `M/director-model-quality.md`; `docs/plans/sdlc-phases.md`, Decisions.
- **reported** — Automatic follow-on files once moved unfinished project work out of the plan. Current add-ons accumulate until a permitted decision routes them to current work, next work or drop. Source: `M/follow-on-work-needs-approval.md`; `commands/unit/add_ons.md`, `<ReviewPendingAddOns/>`.
- **reported** — Phone text, credits text, reminder text, permission text and applied-rename attempts logs record repeated repair or review rounds. These are fix-rounds-unit's evidence; this unit lists them without studying the rounds. Source: `M/MEMORY.md` entries for `attempts-phone-text-review.md`, `attempts-credits-alert-texts.md`, `attempts-codex-reminder-text.md`, `attempts-permission-prompt-alert-text.md`, `attempts-applied-rename-check.md`.
- **reported** — Daemon-family and lint-rewrite attempts logs also concern repair attempts. Their causes and round rules belong to fix-rounds-unit. Source: `M/MEMORY.md` entries for `attempts-daemon-family-escape.md`, `attempts-lint-rewrite-flags.md`.
- **reported** — A general per-turn usage metric is reported to take longer than its report limit, so it can block a useful overhead check. The usage-ledger owner must build that optimization. Source: `docs/plans/build-followups-usage-ledger-next.md`, custom metric item.
- **reported** — An orphan app-server can survive when its own watcher is missing. The saved add-on proposes a periodic sweep; it does not belong to this phase-map implementation. Source: `docs/plans/build-followups-usage-ledger-next.md`, periodic app-server sweep item.
- **reported** — Shared modules imported by two names require type-identity workarounds across consumers. Narrow phase checks can miss those consumers. Source: `docs/plans/build-followups-two-accounts-next.md`, shared whoami imports item; `M/shared-module-full-gate.md`.
- **reported** — A rule scope or date-only edit makes old style results stale and causes another full check. Style owns the saved change; the process map must not count those repeated checks as new implementation. Source: `docs/plans/build-followups-style-next.md`, rule scope item.
- **reported** — A declined style decision needs a hand edit before it can receive a new answer. Style owns the command that the saved add-on requests. Source: `docs/plans/build-followups-style-next.md`, declined decision item.
- **reported** — A grandparent's expanded dailies can omit the child style production's line. The saved add-on shows a status gap across document and production levels; showrunner tooling owns it. Source: `docs/plans/build-followups-style-next.md`, grandchild style production item.
- **reported** — Review commands use different output formats and storage lives. Some outputs remain in scratch space or a transient decision doc. A git-only pipeline measure cannot recover those steps unless the plan names retained evidence. Source: `commands/team_review.md`, `<EstablishWorkingDoc/>`; `commands/adversarial.md`, Frame / Report; `commands/astra_review.md`, Steps; `commands/adhoc_review.md`, steps 2 and 5; `commands/api_review.md`, `<WriteDoc/>`; `commands/module_review.md`, `<WritePlacementPlan/>`.
- **reported** — No common design-doc format or design-writing command appears in the mapped contracts. The plan compiler normally replaces its design input in place; keeping both needs `--out`. This can hide the boundary between design and compiled plan in git history. Source: `commands/plan/to_phased_plan.md`, Purpose / Usage / `<Locate/>` / `<Rewrite/>`; `docs/plans/sdlc-phases.md`, Phase 1.
- **reported** — As-built creation needs the entire plan complete and can delete or move the plan while it fixes peer docs and links. A pipeline reader must follow old plan paths and distinguish current docs from plans still in work. Source: `commands/plan/to_as_built.md`, `<Verify/>`, `<Relocate/>`, `<ReconcileAsBuilt/>`; `docs/delegate/final_gate_commit.md`, `<RunAsBuilt/>`.
- **reported** — Phase start occurs after prompt compose; phase finish occurs before shrink in normal loop mode. A measure from phase start to phase finish alone cannot show the full cost of one conceptual phase. Source: `docs/delegate/launch_implementation.md`, step 2; `docs/delegate/phase_end.md`, steps 5–7; `commands/unit/direct.md`, `<PassOwnership/>`.

## Questions for the user

Answered 2026-10-28 (sdlc-production.md, Review decisions PH2): director tokens
first, and Step 1 is cheap A. C is not built now. No question stays open.

## Candidates

These are proposals. None changes a live process rule. The five time targets
are document lead time, the gap to the next phase, uncovered director time,
implementation, and test. Rank these by total time in Friction, not by median.
For director tokens, use three targets: total input, the share after the last
pass, and the share before the first pass. Friction reports these in one line;
the Baseline gives them separate rows. Shares locate work; they are not token
totals and cannot be added to the input total. Fix worker time is outside this
candidate search.

| Target and Baseline row | Candidates that address it |
| --- | --- |
| Uncovered director time: `Phase steps → director time` | A, B, G, H, I, K |
| Gap: `Phase end through next Work Order → phase end to next phase start` | A, C, D, G, H, I |
| Documents: `Doc pipeline summary → first doc to first phase` | E, F |
| Implementation elapsed: friction source `Phase steps → pass: implementation`; acceptance uses unallocated passes | G, H |
| Test/check elapsed: friction source `Phase steps → pass: test`; acceptance uses unallocated pass/check spans | I, J |
| Total input: `Director tokens summary → input per phase` | A, B, E, F, G, H, I, J, K |
| After the last pass: `Director tokens summary → after last pass share` | A, B, K |
| Before the first pass: `Director tokens summary → before first pass share` | A, E, F, G, H |

The build costs below are author estimates, not measured results. They cover
base implementation and fixture checks. Steps adds recording and measurement
work to each total. Observation wait time is outside those costs. The estimates
assume one writer and one checker. No trial has run (n=0). Hours describe work,
not a promise about elapsed delivery time. A larger build cost does not excuse
a worse result under the three gods.

**A. Read each contract at its step.** Keep `commands/unit/direct.md` as a short
route through the phase. Move the full rules to one named file per step under
`docs/delegate/`. Remove repeated full reads of the same rules from
`commands/unit/checkpoint.md`, `commands/plan/phase_review.md`, and
`commands/plan/shrink.md`; each route still names every required rule. The
director reads the full step file before that step and reads it again when its
content changes. This affects director context, not decision authority. Build:
8–12 work hours; risk: a moved rule or link can be lost. A tag inventory must
prove that every old rule has exactly one authoritative home.

The proposed new files for A are `docs/delegate/run_contract.md` (core rules,
authority, turn end, reservations, progress, and compaction),
`docs/delegate/worker_runtime.md` (worker life, dispatch, waits, and failures),
`docs/delegate/verification_contract.md` (checks, smoke, and UX),
`docs/delegate/findings_contract.md` (ledger, synthesis, and repair routing),
`docs/delegate/phase_cleanup.md` (both cleanup halves and completion), and
`docs/delegate/next_phase.md` (next selection, mode gates, and standing refill).
Keep existing point-of-use step files. Move tag bodies intact; a repair-rule
body stays fix-rounds-unit's content. The route points to it without changing
its rule. A's Files includes these six files, the four existing command
files named in A, the two measure files, and these existing callers:
`docs/delegate/compose_work_order.md`, `docs/delegate/write_prompt_contract.md`,
`docs/delegate/launch_implementation.md`, `docs/delegate/dual_review.md`,
`docs/delegate/phase_checks.md`, `docs/delegate/run_phase_review.md`,
`docs/delegate/phase_end.md`, and `docs/delegate/final_gate_commit.md`.
The caller edits update routes and read cadence; they do not rewrite the rules.

**B. Use one closeout fact set.** In `docs/delegate/phase_end.md`,
`docs/delegate/run_phase_review.md`, `commands/unit/checkpoint.md`,
`commands/plan/phase_review.md`, and `commands/plan/shrink.md`, make checkpoint,
remaining-plan review, and shrink consume one temporary fact set. Gather it
once from the diff, ledger, and check results. Keep the code checkpoint and
notice before plan review, and keep the shrink commit separate. Recheck facts
when the reviewed content, ledger, or check evidence changes. A new commit
with the same content does not make the code evidence stale. This affects the director and doc checker.
Build: 20–28 work hours; risk: stale facts can hide a defect. Retain independent
review, ledger closure, and validation; reduce repeated collection and prose.

**C. Prepare the next Work Order during the review wait.** Change
`commands/unit/direct.md`, `docs/delegate/compose_work_order.md`, and
`docs/delegate/dual_review.md` to permit a read-only draft in phase scratch
space while reviewers run. After review and plan review finish, compare the
draft's inputs with the current Work Order, decisions, and tree. Rebuild on any
change. Dispatch still waits for all existing gates. This affects the director;
it does not move D10 in the roles Duty matrix. Build: 8–12 work hours; risk:
wasted drafts and distraction from the director's main review. No plan write
occurs during a review. The proposal needs guilds-unit's check for review focus.

**D. Assemble the prompt from validated fields.** Change
`docs/delegate/compose_work_order.md`, `docs/delegate/write_prompt_contract.md`,
and a new `scripts/direct/prompt_assembly.py` with fixture tests to assemble
the repeated prompt envelope from the validated Work Order and Delegation
Context. Reuse `scripts/berth/work_order.py` for the source validation.
The director still reads
the resulting prompt and resolves decisions. A changed input invalidates the
assembled result. This affects directors and workers. Build: 16–24 work hours;
risk: assembly can preserve an omission from the source. Existing Work Order
validation remains a gate; assembly does not infer missing scope.

**E. Give design docs one format and one writing command.** Add
`docs/design_format.md` and `commands/plan/design.md`. Update
`commands/plan/to_phased_plan.md` to accept a resolved design with goals,
evidence, decisions, affected states, interfaces, validation, and source links.
Retain the design through `--out` when compiling. Review commands keep their
own scratch outputs; the design records their disposition and retained source,
not a second copy of every review. This affects design authors and compilers.
Build: 16–24 work hours; risk: a form can add work to a small task. Sections can
state why they do not apply. Guilds-unit defines who writes and reviews it.

**F. Compile a small proposal issue directly.** Update
`commands/plan/to_phased_plan.md`, `commands/showrunner/proposal.md`, and
`docs/delegate_plan_format.md` to accept an approved issue as the source when
it already states behavior, files, decisions, and validation. The compiler
builds a full Work Order; missing design facts take the normal design path.
This removes a document transition, not review or user approval. It affects
the proposal author, compiler, and director. Build: 8–12 work hours; risk:
misclassifying work that needs design. Do not choose by file count alone.
Guilds-unit must confirm the review contract before this path goes live.

**G. Put verified code context in the Work Order once.** Tighten
`commands/plan/to_phased_plan.md`, `docs/delegate_plan_format.md`, and
`scripts/berth/work_order.py` and `scripts/berth/tests/test_work_order.py`:
name the relevant entry points, invariants,
callers, and exact checks with the source commit. Workers check whether those
facts still hold before changing code. This affects compilers and workers;
the director still accepts the Work Order. Build: 12–20 work hours; risk:
context decays after a merge. A mismatch requires a source read and correction,
not trust in a stale line number. This extends the compiler's existing
research-once rule; it does not create another design authority.

**H. State independent file partitions before dispatch.** Tighten
`docs/delegate_plan_format.md`, `docs/delegate/write_prompt_contract.md`, and
`scripts/berth/work_order.py` and `scripts/berth/tests/test_work_order.py`:
require actual owner paths, one owner for each
hub file, and a real checker lane. Use `2 writers` only when independent writes
exist. This affects director assignment and workers. Build: 8–16 work hours;
risk: a plan can force false parallel work. Keep `1 writer + 1 tester` when
that is the real partition. D11 and D12 keep their current owners; this changes
the input contract, not a responsibility or a name.

**I. Name exact checks when compiling the plan.** Update
`commands/plan/to_phased_plan.md`, `docs/delegate/phase_checks.md`, and
`docs/delegate_plan_format.md` so the Work Order names the required root gate
and the smallest useful early checks. Workers run the early checks once, then
the full required gate at its existing point. A changed tree invalidates a
prior result. This affects compilers, workers, and directors. Build: 8–12 work
hours; risk: a narrow early check can be mistaken for acceptance. The root gate
still covers shared callers. No test, lint, or closure requirement is removed.

**J. Run independent required checks together.** Update
`docs/delegate/phase_checks.md` and `docs/delegate/write_prompt_contract.md` to
declare which listed checks can overlap and which share mutable state. Start
only the independent ones together through the existing run service, read
every result, and keep the full gate. This affects workers and director
acceptance. Build: 12–20 work hours; risk: CPU or shared-state contention can
make tests slower. Keep ordered checks where the independence claim fails.
This changes scheduling, not test coverage or repair policy.

**K. Format closeout facts from existing records.** As an alternative to B,
add `scripts/direct/closeout_report.py` and fixture tests. Update
`commands/unit/checkpoint.md`, `commands/unit/phase_report.md`, and
`docs/delegate/run_phase_review.md` to use its read-only summary of hashes,
checks, ledger state, and recorded timings. It formats evidence; the director
still judges remaining Work Orders and writes as-built claims. Use named
states such as `ChecksNotRecorded` and `CheckpointVerified`; do not encode
missing evidence as `None`, zero, or a success. It affects the director and
doc checker. Build: 16–24 work hours; risk: another formatter becomes a second
source of truth. It must keep no store and must link every fact to its owner.

All duty checks refer to `origin/sdlc-roles:docs/plans/sdlc-roles-design.md`,
Duty matrix D05, D10–D15, D17, and D27–D28. No candidate moves those duties.
Any later proposal to give compose, acceptance, design, review, or checkpoint
to another role belongs to roles-unit and goes through the showrunner.
The guilds requests below are pending contract changes, not authority to apply them.
No candidate changes repair rounds or fix-time accounting.

The guilds input is the sibling worktree
`/home/natepiano/worktrees/claude-build-followups-sdlc-guilds`, git HEAD
`bf2a60c430aa54f20217103a223b67e6d786364d`,
`docs/plans/sdlc-guilds-design.md`, Command changes, read for this repair.
The table records each request and its destination. Preserve the current rules
until the showrunner supplies the required decisions. A file move must carry
an approved edit with its tag; it must not settle the request by accident.

| Guilds request and defining tag | Affected candidates and destination | Disposition before deployment |
| --- | --- | --- |
| Authorship: direct.md introduction | A route header; E design command; F issue route | Pending guilds/roles decision through showrunner; authorship stays with its approved owner |
| Lasting pair: `<LongLivedWorkers/>` | A `worker_runtime.md`; H prompt partition | Pending guilds lifecycle implementation; retain same-thread rules and do not create a tester for a design workflow |
| Exact writing binding: `<CoordinateDelegatedPhaseReservation/>`, `<DelegatedPhaseReservationContract/>` | A `run_contract.md`; H validator and prompt | Pending guilds reservation bridge; preserve exact output paths and idle rebind requirement when approved |
| Baseline/pass and bound designer: `<PassOwnership/>`, `<LaunchImplementation/>` | A `worker_runtime.md` and launch caller; D envelope; G/H prompts | Pending guilds registry integration; normal implementation dispatch stays unchanged |
| Sole lasting design reviewer: `<EarlyReviewArm/>`, `<DualReview/>` | A worker/review route; C review wait; E/F review input | Pending guilds design route; retain implementation review lenses; C must keep designer review focus |
| Brief ruling and same designer: `<Synthesize/>`, `<FixDispatch/>` | A `findings_contract.md`; B/K fact summary; E/F disposition | Pending guilds design revision route and fix-rounds owner check; no authority or repair policy moves |
| Clear binding after release; retain pair: `<PhaseCleanup/>`, `<FinalGate/>`, `<RunSummary/>` | A cleanup/final routes; B/K closeout | Pending guilds lifecycle bridge; preserve checkpoint release order and durable pair ownership |
| Showrunner design authority: produce.md `<DesignAuthority/>`, `<DesignCheck/>`; production format Words / `<ProductionUnit/>` | E/F source/review contract; A authority links | Destination roles-unit through showrunner; this unit changes no role or name |
| Guild delivery: commands/message.md and codex/AGENTS-messaging.md | No candidate edits these files | Destination guilds-unit and the named hub owners; keep pending/refusal/receipt meanings unchanged |
| Held reviewer selection: commands/astra_review.md Usage / Steps | E/F retained review evidence; A review links | Destination guilds-unit and command owner; keep saved review and ruling links; no extra review thread |


## Comparison

All cuts and build hours below are planning estimates, n=0 trials, for future
before/after windows defined in Steps. They are not Baseline results. A zero
means no direct saving is proposed. It can still regress. Token targets apply
to median input and output separately. Input includes caches; it is not a cash
estimate. Before/after share targets use percentage points (pp). A share cut
alone never passes a token test. Missing evidence is not zero saving.

W = complete phase elapsed time; D = allocated uncovered director time;
I/T/R = allocated implementation/test/review shares; activities are also
allocated. Every unchanged activity, including verification, smoke and UX,
has target 0. Fix worker time and repair counts have target 0 for all candidates.
G/H/I/J have no acceptance target on allocated shares. Their elapsed measures
below use complete, unallocated intervals; each also requires no regression in
phase elapsed time. Overlap can change shares without changing work duration.

U = user approval before live edits; S/F/B = Simple/Fast/Beautiful with
correctness first; E = decision economy; O = guilds/roles/fix ownership;
Q = judgment; M = read-only measure without another fact store;
T = semantic types, fixture tests and full gates; L = clear English/user-zone
times. K means kept. C means conditional on the stated check. A broken user
rule rejects the candidate. The guilds dispositions above apply to each row.
The proposed exposure recorder extends the existing event source; the measure
remains read-only. No model downgrade or responsibility change is proposed.

| Candidate and cause | Phase measures: expected median cut | Finish-to-start gap cut | Director input / output cut | Before / after share cut | Static full-read size; scheduled read load | Doc→run / run→phase / doc→phase cut | Churn: Work Order edits / opened / closed / inserted / splits | Quality check | Rules U; S/F/B; E; O; Q; M; T; L |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A: read unchanged rules once per route | W 3%, D 10%; I/T/R/activities/fix 0 | 10 s: fewer compose reads | 10% / 10% | 2 / 2 pp | No static line/byte cut; six extra files and bounded route text. Scheduled byte cut at least 19.8% on the route below; no line-cut target | 0 / 0 / 0 | 0 / 0 / 0 / 0 / 0 | Every tag and every mode gate retained | K; C/K/K; K; C; K; K; K; K |
| B: reuse closeout facts | W 5%, D 15%; I/T/R/fix 0; shrink elapsed 20%, other activities 0 | 0 s: checkpoint/review precede finish; loop shrink follows next launch | 15% / 15% | 0 / 6 pp | No static line/byte or scheduled-read cut claimed | 0 / 0 / 0 | 0 / 0 / 0 / 0 / 0 | Content/ledger/check invalidation; independent review and both commits retained | K; K/K/K; K; C; K; K; K; K |
| C: compose during review wait | W/D/I/T/R/activities/fix 0 | 60 s: draft precedes gap | 0% / 0%; invalidated drafts can add tokens | 0 / 0 pp | No static line/byte or scheduled-read cut claimed; draft guard adds text | 0 / 0 / 0 | 0 / 0 / 0 / 0 / 0 | Main review focus; no tree writes; stale draft rejected | K; C/K/K; K; C; C; K; K; K |
| D: assemble validated envelope | W/D/I/T/R/activities/fix 0 | 45 s: less compose writing | 0% / 0%; savings before phase start are not phase tokens | 0 / 0 pp | No static line/byte or scheduled-read cut claimed; adds script/schema | 0 / 0 / 0 | 0 / 0 / 0 / 0 / 0 | Director reads output; missing fields block dispatch | K; K/K/K; K; C; K; K; K; K |
| E: resolved design format | W/D/I/T/R/activities/fix 0 | 0 s on later gaps | 4% / 4% on first phases only | 2 / 0 pp | No static line/byte or scheduled-read cut claimed; adds design format/command | 20% / 0 / 20% | 20% / 0 / 0 / 10% / 10% | Retain states, decisions, sources and required review | K; C/K/K; K; C; C; K; K; K |
| F: complete small issue compiles directly | W/D/I/T/R/activities/fix 0 | 0 s on later gaps | 2% / 2% on first phases only | 1 / 0 pp | No static line/byte or scheduled-read cut claimed; eligibility adds text | 30% / 0 / 30% | 0 / 0 / 0 / 0 / 0 | Incomplete source takes normal design path | K; C/K/K; K; C; C; K; K; K |
| G: verified context once | W 3%, D 3%; allocated I/T/R/activities/fix 0; unallocated implementation pass median 10% | 15 s: source context aids compose | 5% / 5% | 3 / 0 pp | No static line/byte or scheduled-read cut claimed; tighter compiler input | 0 / 0 / 0; compile cost can rise | 15% / 0 / 0 / 10% / 10% | Source mismatch requires fresh read; phase elapsed cannot regress | K; K/K/K; K; C; K; K; K; K |
| H: independent file owners | W 4%, D 2%; allocated I/T/R/activities/fix 0; unallocated implementation pass median 15% | 5 s: fewer assignment corrections | 3% / 3% | 1 / 0 pp | No static line/byte or scheduled-read cut claimed; clearer partition text | 0 / 0 / 0 | 10% / 0 / 0 / 0 / 0 | One hub owner, real lanes; phase elapsed cannot regress | K; K/K/K; K; C; K; K; K; K |
| I: exact early checks | W 1%, D 1%; allocated I/T/R/activities/fix 0; unallocated test pass and check-span medians 10% | 5 s: check list already known | 1% / 1% | 0 / 0 pp | No static line/byte or scheduled-read cut claimed; exact commands added | 0 / 0 / 0 | 5% / 0 / 0 / 0 / 0 | Same root gate; phase elapsed cannot regress | K; K/K/K; K; K; K; K; K; K |
| J: overlap independent checks | W 2%; D/allocated I/T/R/activities/fix 0; unallocated required-check span median 15%; per-check duration target 0 | 0 s: testing precedes closeout | 1% / 1% | 0 / 0 pp | No static line/byte or scheduled-read cut claimed; scheduling guard added | 0 / 0 / 0 | 0 / 0 / 0 / 0 / 0 | Every check/result; no per-check or phase elapsed regression | K; C/C/K; K; K; K; K; K; K |
| K: read-only closeout formatter, alternative to B | W 2%, D 6%; I/T/R/activities/fix 0 | 0 s: no mechanism in measured gap | 6% / 6% | 0 / 3 pp | No static line/byte or scheduled-read cut claimed; adds formatter/schema | 0 / 0 / 0 | 0 / 0 / 0 / 0 / 0 | Missing facts visible; director judges | K; C/K/K; K; C; K; K; K; K |

The I/T target rows in Candidates name the friction that G/H/I/J address.
Their Comparison score is positive on the new unallocated elapsed measure,
not on the old allocated share. Use shares as diagnostics only. R and each
activity remain scored even where acceptance moves to a different measure.
For B, link shrink to the completed phase and also report the phase whose
recorded interval contains it. Normal loop shrink occurs after the next launch.
Do not assign that saving to the completed phase's finish-to-start gap.
For E/F, the total doc-lead cut is a hypothesis, not the sum of component cuts.

A needs a read denominator before approval. The following schedule defines a
single ordinary loop route: one implementation prompt, two review prompts,
one code checkpoint, plan review, and shrink beside the next workers. No repair,
compaction, extra prompt, mode gate or final closeout occurs. The old column
counts required reads, not observed file opens. File sizes come from Baseline,
Contract full reads; the grouped bytes are arithmetic from its minimum sum.
The proposed column is a byte budget, n=0 built routes, not a measured revision.
The old file sizes use the printed Baseline window and its n=1 per named file;
the grouped row has n=5 files. Schedule totals have n=1 proposed route. Future
trials must print their actual before/after windows separately.

| Route step; files read | Old required schedule: bytes | Proposed A schedule: bytes |
| --- | --- | --- |
| Start: commands/unit/direct.md | Full file once: 92,400 | Router once: at most 4,096; original rule bodies move to the six files below |
| Core at start; runtime before dispatch; verification before checks; findings before synthesis; cleanup before completion; next-phase before selection | Bodies are within direct.md above | run_contract.md, worker_runtime.md, verification_contract.md, findings_contract.md, phase_cleanup.md, next_phase.md: aggregate at most 92,400; read each at its named step once |
| Prompt: docs/delegate/write_prompt_contract.md before implementation and each of two review prompts | Three reads at 23,224: 69,672 | One full read: 23,224; reuse only its recorded unchanged revision for the other prompts |
| Review: docs/delegate/dual_review.md | One read: 16,188 | One read: 16,188 |
| Code checkpoint and later shrink commit: commands/unit/checkpoint.md | Two reads at 17,964: 35,928 | One full read: 17,964; unchanged revision reused at shrink commit |
| Plan review: commands/plan/phase_review.md | One read: 33,476 | One read: 33,476 |
| Shrink: commands/plan/shrink.md | One read: 24,125 | One read: 24,125 |
| Dispatch: docs/delegate/launch_implementation.md | One read: 5,678 | One read: 5,678 |
| Add-on check: commands/unit/add_ons.md | One read: 6,268 | One read: 6,268 |
| Other required files at compose/checks/review/phase end: docs/delegate/compose_work_order.md, phase_checks.md, run_phase_review.md, phase_end.md; commands/unit/style_review.md | Each once: aggregate 20,118 | Each once: aggregate 20,118 |
| Full ordinary loop route, including shrink after next start | 303,853 | At most 243,537; at least 60,316 fewer bytes, or 19.8% |

The old minimum in Baseline is 239,441 bytes with each required file once.
This schedule adds two prompt reads and one checkpoint read. The proposed
schedule retains all unique rule bodies and adds the router budget. All new
headers and read guards must fit those budgets; deleting a rule to fit is forbidden.
Static unique bytes have that router-budget increase as a design cap;
static lines have no saving target. The exact new line and byte counts must
come from the built revision. The earlier 35% target is withdrawn.
Recording support can change the old-route file sizes. Recompute both columns
from the selected prospective old/new revisions before freezing the trial.
If complete rules cannot fit the caps, revise the budget and expected read cut
through the showrunner before deployment; do not treat these estimates as facts.
Extra prompts, changed content and compaction require new reads and separate
schedule rows. Never reuse a read across compaction or a changed revision.
Mode-specific routes add brief/report/CI/final and exception rules before they
qualify. Keep the old full-read cadence outside this specified loop route until
a separately approved schedule covers it. This example claims no other route saving.

Extend phase_process.py and its fixture tests to accept a selected contract
repository and revision independently of the measurement code revision.
Read blobs from that revision; reject an uncommitted or missing revision selector
rather than silently reading the measure's checkout. For each route, print
file paths, blob hashes, lines, bytes, cadence, total scheduled reads and the
static unique-size view. Dirty consumed inputs remain separate exposure
artifacts; they do not become a fake git revision. The proposed six-file
aggregate must expand into individual blob sizes when built. Fixture tests pin identical content in
different layouts, repeated reads, changed revisions and compaction rereads.

Rankings use future matched samples, n=0 trials today. Existing-phase rows and
new-plan rows have separate rankings; there is no common population weight
that would support a combined numerical order. Even within existing phases,
route eligibility differs. Read the orders as target priorities within each
eligible class, not measured portfolio benefit. Each entry shows token and
finish-to-start gap cuts. Ties favor lower total build cost.

| Existing-phase rank | Director tokens first: token cut; gap cut | Dispatch time first: token cut; gap cut |
| --- | --- | --- |
| 1 | B: 15%; 0 s | C: 0%; 60 s |
| 2 | A: 10%; 10 s | D: 0% inside phases; 45 s |
| 3 | K: 6%; 0 s; alternative to B | G: 5%; 15 s |
| 4 | G: 5%; 15 s | A: 10%; 10 s |
| 5 | H: 3%; 5 s | H: 3%; 5 s |
| 6 | I: 1%; 5 s | I: 1%; 5 s |
| 7 | J: 1%; 0 s; check span 15% | B: 15%; 0 s |
| 8 | D: 0% inside phases; 45 s | K: 6%; 0 s; alternative to B |
| 9 | C: 0%; 60 s | J: 1%; 0 s; check span 15% |

| New-plan rank | Director tokens first: first-phase token cut; first-doc-to-dispatch cut | Dispatch time first: first-phase token cut; first-doc-to-dispatch cut |
| --- | --- | --- |
| 1 | E: 4%; 20% | F: 2%; 30% |
| 2 | F: 2%; 30% | E: 4%; 20% |

These new-plan orders apply only where the same source meets both candidates'
content rules. Otherwise select by eligibility, not by this rank. Do not apply
the percentages to Baseline's multi-day tail. B and K are alternatives. All
other effects can overlap; there is no summed forecast for a candidate set.

Including the recording work below changes the proposed first step. B has the
largest central token target per work hour among existing-phase proposals:
15% over 28–40 h, versus A's 10% over 24–36 h. These are estimates, n=0 trials;
the cost ranges overlap and eligible populations are not yet recorded. The
claim is a planning choice, not a proved benefit ratio. B keeps the route layout
and reduces repeated closeout collection; A must first build revision-aware
read accounting. E/F remain separate because their first-phase population
has no measured weight against later phases. Recommend B as Step 1, with a
prospective old-route sample before deploying its fact reuse.

## Recommendation

Director tokens first, with judgment quality held. The user chose this on
2026-10-28. Step 1 is cheap A: `commands/unit/direct.md` becomes a short route,
and each step reads only its own rule file. After a compaction the director
reads the always-read rules and the current step's file, found by tag name.

The adversarial review (for, against and a judge, all evidence scripts re-run)
changed the A and C numbers in Comparison. These values replace them:

| Option | Director token cut | Gap cut | Work hours |
| --- | --- | --- | --- |
| Cheap A | 3–7% | 0–3 s | 2–4 |
| Full A | 4–8% (counted and weighted) | 0–3 s | 10–16 with merges |
| C | 0 to −2% | 15–50 s per eligible gap; 20–50% of gaps are eligible | 8–12 |

The 10% token and 10 s gap targets for A do not hold. Inserted phases start
about 2% of gaps (1.3–3.1%), not 28.2%: most letter ids are standing-plan
tickets. `direct.md` costs about 41k tokens as a Read (35k as raw text).
Cheap A's guards: it does not rely on the 20k restore after compaction, the
core rules are always read, each tag is found by its name, the tag inventory
stays complete, and no `/unit:delegate` alias copies the text.

The settling test is a 20-phase trial of cheap A. It measures context size
just after the reload, compactions per phase, and weighted tokens over each
phase plus its following gap. Phase 4 runs it. B, full A, G and I stay
candidates for later answers; C waits until dispatch time comes first.
Astra review `/tmp/claude/astra/20261020T222712Z-2504835/review.md` and the
adversarial verdict are folded into this document.

## Steps

Every step needs a separate user answer before live edits. Phase 3 copies the
approved files, target, recording work and undo into its Work Order. These
steps propose process and recorder changes; none is applied by this document.
The adversarial result is in Recommendation. Guilds destinations above stay
pending until the showrunner supplies their decisions.

Common trial work is part of each total estimate below, not free filtering.
Add exposure evidence to scripts/direct/progress_history.py and fixture tests
in scripts/direct/test_progress_history.py. Wire the capture calls into
commands/unit/direct.md, docs/delegate/compose_work_order.md,
docs/delegate/write_prompt_contract.md and docs/delegate/phase_end.md.
Add a recorder read-contract operation that serves the exact selected bytes
and records their hash with the step. Test dirty content and changed revisions.
Call it at a full-read boundary; a manual statement that a file was read is not
revision evidence. At prompt/compile/check/closeout boundaries, record the resolved
contract root, immutable revision plus file blob hashes, route/mode, run and
phase instance, input prompt or plan hash, check-list hash and boundary time.
A hash of the implementation worktree does not identify ~/.claude's contracts.
If files are dirty, retain the exact consumed bytes in the existing phase
prompt artifact and record its content hash/path; a path without retained
bytes is not evidence. No extra measure database is created.

Expose those existing events to phase_process.py with fixture tests in
test_phase_process.py. Define TrialExposure as BeforeChange, AfterChange or
ExposureNotRecorded; retain the reason in the last state. No None, zero or
inferred deploy timestamp stands for missing exposure. Add exact unit, mode,
phase/plan class and exposure filters; read the selected contract revision
separately from the measurement version. Freeze that version for both samples.
For already cached text, retain the read event that served that exact revision;
compaction forces a new served read. This records delivered text, not proof
that a model understood it; required review and gates still check behavior.
Common work costs 8–12 estimated work hours per standalone candidate, n=0
trials; reuse it in later steps only after its evidence fits that step. Totals
below conservatively include it each time and exclude observation wait time.

The current run_started event retains working_dir, worktree, branch, plan_doc
and main_agent. Phase and pass events retain ids and observed time bounds.
Usage.db retains mapped director turns. Git retains committed plan content and
first source commits. Existing phase prompts and check logs supply facts only
where their actual bytes remain. These sources can select unit, ids, recorded
pass times, model, file partition and named check commands. They do not record
the consumed contract revision, compiler route or checklist revision.
Do not reconstruct those missing facts from the implementation commit.

For every selected step, first deploy only its approved recording support with
the old process behavior. Then collect a prospective old-route sample, including
its actual ~/.claude inputs. Freeze that before window before deploying the
candidate behavior. After the showrunner names its deploy hash/time, collect
the matching after sample. A deploy time selects a possible window; consumed
revision evidence selects actual exposure. Keep other candidate exposure fixed.
No change caused by a moving Baseline window is a saving. If evidence is absent, report
ExposureNotRecorded and its n. Do not quietly substitute a different route.

All trial windows are [since, until) in the user zone (`scripts/lib/user_zone.py`). Phase trials need at
least 20 completed unmixed phases from at least two units present in both
windows. Show unit results separately; match mode, scope class, model and gates.
A new-plan trial needs at least 10 eligible plans per window, with matched
source content classes. These are collection thresholds, not statistical proof.
Print window, n, missing count, median, p90 and totals for each measure. Below
the threshold or with missing exposure, report not yet measurable and the next
collection condition. Do not call that outcome a failed saving.

The named median cut is the primary target. Less than half of that target
requires undo or a revised proposal through the showrunner. Every trial also
requires no regression in median or p90 phase elapsed time in either unit,
and no loss of judgment, main/independent review, ledger/closure, smoke or
required full checks. Changed workload or gates requires a matched sample or
an inconclusive result, never an apparent success. Unchanged finding counts
alone do not prove judgment. Keep all evidence when undoing a process change.

For G/H, acceptance uses each complete implementation pass's finish minus
start, without dividing by concurrent labels; report each lane class apart.
For I, require both the complete test-pass duration and required-check span
(first required check start to last required check finish) to improve by its
target. For J, accept only a shorter required-check span, retaining individual
check elapsed times and worker-time totals; no individual-check median/p90
may regress. Pass bounds exist in the recorder. Exact per-check times may
exist in mesh_runs logs; do not assume their presence. Add required check
identity, input tree hash, machine, start, finish and result references through
the existing recorder and docs/delegate/phase_checks.md before the prospective
old-route sample when absent. This elapsed-measure work costs a further
4–8 estimated hours for G/H/I/J and is in their totals. Fixture tests must
show that a fixed-duration pass overlapping another label does not get faster.
Allocated shares remain diagnostics with no acceptance threshold.

| Candidate | Evidence for its eligibility beyond common ids/mode/model | Required prospective record before old-route collection | Total estimated work hours, n=0 trials |
| --- | --- | --- | --- |
| B | Code/shrink commit hashes from git; closeout input hashes and route are absent | phase_end/checkpoint/run_phase_review/shrink calls record source phase, containing phase, route and evidence hash at each use; only then collect old closeout route | 28–40 |
| A | Prompt count from retained prompts/pass events; consumed read revisions are absent | Actual file read/step and revision events from direct and all A callers; expand both read schedules from selected revision, including exceptions | 24–36: base 8–12, common 8–12, schedule/accounting 8–12 |
| G | Work Order/source commit and file targets from retained prompt/git; compiler version is absent | to_phased_plan records source blobs, compiler/format revision and output hash in the plan header and existing run event; retain mismatch disposition | 24–40: base 12–20, common 8–12, elapsed 4–8 |
| I | Exact commands in prompt and finished logs where retained; checklist version and complete timings can be absent | Compiler and phase_checks record checklist hash, input tree and check boundaries; preserve gate identity | 20–32: base 8–12, common 8–12, elapsed 4–8 |
| C | Adjacent phase/review bounds exist; review opportunity, blocks and draft inputs are absent | dual_review/compose record next-order eligibility, user/dependency blocks and current source hash in both samples; after also record draft hash/invalidation and cost | 16–24 |
| D | Validated Work Order artifact exists where retained; assembly version is absent | compose/write_prompt record manual old-envelope version and inputs, then assembly revision and same inputs after | 24–36 |
| E | Named git source commits and first phase bounds exist; format/review route version is absent | Existing compiler records old input/source/output hashes and review links; new design command also records its format after deployment; both retain source content class in plan headers/events | 24–36 |
| F | Issue content/approval links can be retained; issue eligibility and compile route are absent | proposal/compiler record complete-source eligibility verdict and review/approval evidence before old-route collection; same rule after | 16–24 |
| H | Prompt Workers/owned paths and hub list exist where retained; validator revision is absent | validator/write_prompt record partition class, hub count, validator and output hash before both samples | 20–36: base 8–16, common 8–12, elapsed 4–8 |
| J | Check set may exist in prompt/logs; independence and machine/timing evidence can be absent | phase_checks/write_prompt record check hashes, independence verdict, machine and every boundary/result before ordered old-route collection | 24–40: base 12–20, common 8–12, elapsed 4–8 |
| K | Same commit/check/ledger sources as B; formatter/closeout route is absent | checkpoint/phase_report/run_phase_review record manual route and source hashes before old-route collection; formatter revision after; no B exposure | 24–36 |

These records are changes to the existing event owner and its callers, not
filters that invent historical exposure. Steps below include all common files,
their row's capture callers and tests, the candidate files, and the selected
measure/filter files. A fresh session must include them in its owned Files.
Contract route decisions remain with the current owners.

1. **Cheap A — Route `direct.md`. Approved by the user (2026-10-28).**
   Make `commands/unit/direct.md` a short route: the always-read core rules,
   then one line per step naming its rule file. Move each rule body into its
   step file as it is; repair rules stay fix-rounds-unit's text and `<Guild>`
   text stays guild-process-unit's. After compaction the director reads the
   core rules and the current step's file by tag name. Keep the tag inventory
   complete; add no `/unit:delegate` copy. Start after fix-rounds-unit's
   `<FixDispatch/>` edits merge. Target: director tokens 3–7% lower per phase;
   gap 0–3 s. Phase 4 runs the 20-phase trial named in Recommendation.
   Total: 2–4 work hours plus the trial's recording. Undo: restore the single
   `direct.md` from git; retain the trial records.

2. **B — Reuse closeout facts. Needs the user.** Keep both commits, notice,
   plan review and completion in their current order. Add B's recording calls
   and collect the old-route sample before enabling reuse. Target median
   director input and output per recorded phase: 15% cut; after-pass share:
   6 pp cut; gap: 0 s. Secondary: D 15%, W 5%, shrink elapsed 20%.
   Select completed unmixed loop phases with code and shrink commits and a
   next phase, mapped director turns, closed pass bounds and confirmed B route.
   Include the same units and containing-phase exposure in both samples; link
   shrink to its source phase and do not move its elapsed time across recorded
   boundaries. Test stale content, ledger/check changes, interruption and
   shrink failure. Total: 28–40 work hours. Undo: restore separate collection
   and old route through an approved checkpoint; retain facts and links.
   Its expected gap benefit is zero.

3. **Full A — Route contract reads. Needs a separate answer after cheap A.** Keep
   cheap A exposure fixed and compare it with full A. Add selected-revision read
   accounting and capture the old schedule before moving text. Target median
   director input/output: 4–8%; gap: 0–3 s; D 10%, W 3%. Scheduled bytes target
   at least 19.8% lower only on the ordinary loop route specified above;
   static lines/bytes have no saving target. Select unmixed completed loop
   phases with exactly the named prompt/checkpoint route, unchanged gates and
   consumed read revisions. Extra dispatch/compaction/mode routes have separate
   samples and schedules. Test every tag, reachable gate, missing blob and
   reread rule. Total: 24–36 h. Undo: restore old routing/read rules and callers;
   retain measurement support, selected revisions and both samples.

4. **G — Compile verified context. Needs a separate answer.** Change G's
   files and compiler recording calls, plus unallocated elapsed measurement.
   Collect old compiler-route evidence before changing requirements. Target
   complete implementation-pass median elapsed: 10% cut; phase elapsed cannot
   regress. Shares are diagnostics. Director input/output: 5%; gap: 15 s;
   W 3%, D 3%; Work Order edits 15%, inserted/split 10%.
   Select code phases with retained compiler/format/source/output hashes,
   comparable file targets and the same prior-step exposure. Exclude pure plan
   reconciliation and compare writer-lane classes apart. Report pass n and phase n separately. Test stale/missing
   source rejection and invariant pass duration under overlap. Total: 24–40 h.
   Undo: restore compiler requirements/validator; keep valid dispatched context
   and trial evidence.

5. **I — Compile exact checks. Needs a separate answer.** Change I's files,
   compiler/check recording calls and elapsed measures. Collect complete old
   ordered-check evidence first. Target test-pass and required-check-span
   medians: both 10% lower; phase elapsed cannot regress. Director input/output:
   1%; gap: 5 s; W 1%, D 1%, Work Order edits 5%. Select code phases with the
   same root gate, machine class, recorded complete test pass and check set,
   checklist/format/input hashes and unchanged prior exposure. Test root-gate
   retention, changed-tree invalidation and overlap-invariant duration.
   Total: 20–32 h. Undo: restore old checklist instructions; keep every gate,
   result and trial record.

6. **C then D — Dispatch-first alternative. Each needs its own answer.**
   C is not built now (user, 2026-10-28). C changes its three files and
   common/opportunity capture calls. Target gap median: 15–50 s lower on
   eligible gaps; phase token/wall targets 0. Select adjacent completed
   unmixed loop phases with a review-wait draft opportunity and no user or
   dependency block, established from recorded current inputs before either
   route runs. Include invalidated drafts, cost and changed orders; do not
   select only successful drafts. Minimum sample is the common phase threshold
   applied to eligible gaps. Pin no writes during review and stale rejection.
   Total C: 16–24 h. Undo: stop drafts; compose after plan review.
   D then needs a separate C-only prospective before sample and envelope input
   capture, with the same adjacent-phase filter and confirmed assembly revision.
   Target gap: 45 s; phase tokens 0%. Pin missing/changed fields and director
   validation. Total D: 24–36 h. Undo: restore manual envelope assembly and
   remove unused assembler; retain Work Order validation. Never add C/D targets.

7. **E or F — New-plan trials. Each needs the user and guilds disposition.**
   Include candidate compiler/format/proposal capture files in addition to its
   base files. First instrument the old route and record prospective source
   eligibility. E targets first-doc-to-first-phase median 20% lower and
   first-phase input/output 4% lower; F targets 30% and 2%. Later gap targets
   are 0 s. Use the same content class before and after, required review,
   named source hashes, fully recorded commits, approval links and compile
   route. Missing commits give NotRecorded lead. Only an earliest named first
   commit after dispatch gives DocAfterDispatch; a later additional doc alone
   does not exclude a valid earlier lead. Report negative and missing leads
   apart. Require the common new-plan sample threshold. Pin missing facts,
   unresolved decisions, source links and required review. Totals E: 24–36 h;
   F: 16–24 h. Undo: restore old compile route; retain approved docs/issues/plans.

8. **H, J or K — Conditional alternatives. Each needs its own answer.**
   H adds partition/validator capture and unallocated pass measurement before
   the old route sample. Select matched code-phase writer/partition/hub-count
   classes, prior exposure and retained output hashes. Target implementation
   pass elapsed median 15%, tokens 3%, gap 5 s; phase elapsed cannot regress.
   Total H: 20–36 h. Pin hub collisions and overlap-invariant durations.
   Undo: restore prior partition validation; retain evidence and hub ownership.
   J adds check capture before the old ordered route sample. Select identical
   check/input/machine classes with recorded independence and results. Target
   unallocated required-check span median 15%, tokens 1%, gap 0 s; individual
   checks and phase median/p90 cannot regress. Report per-check and worker
   totals apart. Total J: 24–40 h. Pin shared-state refusal and the same-duration
   overlap fixture. Undo: restore ordered launches, retaining all checks.
   K uses B's population without B exposure; record old manual route and source
   hashes before adding its formatter. Target tokens 6%, after share 3 pp,
   gap 0 s; W 2%, D 6%. Total K: 24–36 h. Pin named missing-evidence states.
   Undo: restore manual formatting and remove unused formatter; retain sources.
   Each uses the common sample threshold, recording files and quality gates.
