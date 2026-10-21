# Phase process and document pipeline

This document maps the current process. It records its cost and its known problems.
It does not approve a change to that process.

Source plan: [sdlc-phases.md](sdlc-phases.md), Decisions and Phase 1.
Repair rounds, phase meaning in run records, and fix time per phase belong to
fix-rounds-unit. Its `scripts/history/rounds.py` is not merged yet. The baseline
keeps one stub row: `fix — from history.py rounds (fix-rounds-unit), not counted here`.
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
The measure uses a replaceable boundary function until fix-rounds-unit supplies
phase meaning and fix costs through `scripts/history/rounds.py`. It keeps no
second store of those facts. This limit applies to step costs, director tokens,
phase counts, and gaps. Source: fix-rounds-unit via the sdlc showrunner, relayed
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
fix-rounds-unit's pending source and counts no fix time here. Activity names pass
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
| 12. Repair and check closure when the ledger requires it | `commands/unit/direct.md`, `<FixDispatch/>`; `docs/delegate/dual_review.md`, `<ClosureReview/>`; `docs/production_format.md`, `<ProductionUnit/>` item 7 | Repair worker; closure reviewer; director; scripts | Complete batch of ids, affected paths and callers, repair diff, gate result | Repairs, required check results, closure verdicts, updated ledger | `Phase steps → fix — from history.py rounds (fix-rounds-unit), not counted here`; closure pass: `Phase steps → pass: review`. Phase meaning and fix costs await that owner |
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

Scope: fix time is the named stub above. Recorded event boundaries are the
temporary input until fix-rounds-unit's `scripts/history/rounds.py` is merged.
Do not read the stub as zero fix cost, or recorded phase counts as plan-phase
counts. Source: sdlc showrunner through the unit director, 2026-10-20.

Command: `python3 scripts/direct/phase_process.py baseline`.
Window: `[2026-10-06 17:58:57 EDT, 2026-10-20 17:58:57 EDT)`.
The real-data run on 2026-10-20 at 17:58 EDT took about 20 s.
`--detail <table>` prints full rows; the saved default output below shows summaries
and top lists. The log's final `exit 0` line is outside this output.
Source: `/tmp/claude/unit/4b421ed5-d048-4689-bd8d-adf04d343705/phases/1/baseline_final.log`.

```text
Phase process baseline — natedev only; no Mac records read. Use baseline --detail TABLE for full rows.
n counts phases, plans or files as labeled; p90 is nearest rank. Top lists show 10 plus summed other.
Distinct overlapping steps share wall time equally; parallel workers of one kind count once. Fix costs are excluded.
Director time includes uncovered waits; unmatched steps have unknown coverage. Activity tails below 0.1% are merged.
Recorded phases can hold multiple plan phases; rounds.py will supply boundaries. Plans are selected by run start, phases by finish.
Token input includes caches; pass records bound before/after shares of input + output. Shared sessions can overlap; compose before phase start is not isolated.

Phase steps — natedev only | [2026-10-06 17:58:57 EDT, 2026-10-20 17:58:57 EDT)
step                                                              n phases  median   p90      total     wall share
director time                                                     1457      328.2s   1.36h    1451.96h  38.8%
pass: implementation                                              1373      446.7s   1.23h    774.00h   20.7%
pass: test                                                        506       406.9s   1.17h    241.81h   6.5%
activity: Verification                                            236       627.1s   1.67h    156.75h   4.2%
pass: review                                                      647       303.3s   1103.6s  84.78h    2.3%
activity: shrink                                                  297       77.7s    228.7s   23.89h    0.6%
activity: UX review                                               107       368.0s   1889.8s  21.12h    0.6%
activity: other (41 labels)                                       76        187.1s   1735.2s  13.87h    0.4%
activity: Mac smoke                                               1         11.77h   11.77h   11.77h    0.3%
activity: smoke                                                   28        53.4s    3361.3s  4.95h     0.1%
fix — from history.py rounds (fix-rounds-unit), not counted here  0         —        —        —         —
phase wall                                                        1457      1446.5s  5.05h    3746.01h  100%

Phase end through next Work Order — natedev only | [2026-10-06 17:58:57 EDT, 2026-10-20 17:58:57 EDT)
step                           n phases  median  p90      total
phase end to next phase start  1319      107.6s  2767.1s  1266.38h

Director tokens summary — natedev only | [2026-10-06 17:58:57 EDT, 2026-10-20 17:58:57 EDT)
measure                  n phases  median     p90         total           not recorded
input per phase          1454      6,473,348  44,975,747  29,039,391,216  3
output per phase         1454      16,062     163,959     97,480,857      3
before first pass share  1417      11.1%      44.0%       —               40
after last pass share    1417      40.2%      73.9%       —               40

Director tokens: top 10 phases by input — natedev only | [2026-10-06 17:58:57 EDT, 2026-10-20 17:58:57 EDT)
run/phase@start                       n phases  input           output      n turns
8f4c2e88/38@2026-10-08 02:17:59 EDT   1         491,573,693     1,514,293   2,545
8f4c2e88/C24@2026-10-12 04:50:48 EDT  1         424,970,980     1,331,340   2,228
6df1f9fc/86@2026-10-08 05:21:07 EDT   1         373,463,151     1,192,232   1,883
f0ad402c/17@2026-10-07 07:05:39 EDT   1         350,978,965     1,354,683   1,728
f80f42eb/20@2026-10-06 17:46:48 EDT   1         323,843,858     1,187,991   1,659
6df1f9fc/66@2026-10-07 05:01:19 EDT   1         322,980,520     1,252,341   1,586
720c2a10/120@2026-10-15 10:44:59 EDT  1         314,060,185     831,357     1,662
a702c130/15@2026-10-06 22:49:59 EDT   1         311,014,536     995,869     1,606
0d0f70cd/65@2026-10-06 17:46:09 EDT   1         305,046,054     1,057,709   1,549
24e20a41/17@2026-10-17 08:25:51 EDT   1         299,101,922     1,226,040   2,247
other (n=1444)                        1444      25,522,357,352  85,537,002  137,321
not recorded (usage.db turns (collect.py): no mapped turns in phase); n phases=3

Contract size summary — static checkout | [2026-10-06 17:58:57 EDT, 2026-10-20 17:58:57 EDT)
measure         n files  median   p90     total    not recorded
lines per file  16       134      540     4,675    0
bytes per file  16       6,103.5  33,476  258,411  0

Contract full reads — static checkout | [2026-10-06 17:58:57 EDT, 2026-10-20 17:58:57 EDT)
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

Doc pipeline summary — natedev only | [2026-10-06 17:58:57 EDT, 2026-10-20 17:58:57 EDT)
measure                   n plans  median  p90     total     not recorded
first doc to run          45       384.3s  38.10h  1248.80h  54
run to first phase        99       84.3s   184.6s  3.16h     0
first doc to first phase  45       451.1s  38.27h  1250.27h  54

Doc pipeline: top 10 plans by lead time — natedev only | [2026-10-06 17:58:57 EDT, 2026-10-20 17:58:57 EDT)
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
other (n=89)                              89       -12.32h       -13.43h     2.79h
Earliest doc requires all named source commits. Document commits not recorded: n=55/120 documents; source: git log. Commands and exact milestones: --detail doc-pipeline.

Plan churn summary — natedev only; first run through window end | [2026-10-06 17:58:57 EDT, 2026-10-20 17:58:57 EDT)
measure             n plans  median  p90  total  not recorded
Work Order edits    37       1       4    58     62
decisions opened    37       0       1    10     62
decisions closed    37       0       1    11     62
phases inserted     37       5       53   545    62
named phase splits  2        4.5     7    9      97

Plan churn: top 10 plans by total changes — natedev only | [2026-10-06 17:58:57 EDT, 2026-10-20 17:58:57 EDT)
plan                                  n plans  total  todo  opened  closed  inserted  splits
tool-based-ui.md                      1        91     1     3       2       85        not recorded
build-followups-two-accounts.md       1        85     17    0       0       68        not recorded
hana-organon-design.md                1        78     0     0       0       78        not recorded
widget-enhancements-plan.md           1        67     3     3       3       51        7
frame-time-plan.md                    1        63     8     1       1       53        not recorded
remove-running-section-tile-fixes.md  1        33     2     3       3       23        2
tool-based-ui-cables.md               1        25     2     0       0       23        not recorded
build-followups-style.md              1        25     4     0       0       21        not recorded
info-panels-plan.md                   1        20     2     0       0       18        not recorded
build-followups-unit-board.md         1        20     0     0       0       20        not recorded
other (n=27)                          27       126    19    0       2       105       not recorded
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
`[2026-10-06 17:58:57 EDT, 2026-10-20 17:58:57 EDT)` and natedev records.
The time lines come first, from largest to smallest total. They use different
samples and can overlap; do not add their totals. Token, read-load, and churn
lines follow because their units differ. The current checkout supplies read
sizes. Doc lead time can start before the window; churn starts at the first run.

- **measured** — Uncovered director time takes 38.8% of phase wall time: 1451.96 h over 1457 recorded phases; median 328.2 s, p90 1.36 h. It includes waits. It is an upper bound on director work in these uncovered intervals, not a measure of active work across the whole phase. Source: Baseline, `Phase steps → director time`.
- **measured** — Phase end to next phase start totals 1266.38 h over 1319 gaps; median 107.6 s, p90 2767.1 s. It can include next Work Order compose, gates, and waits; it is not all closeout work. Source: Baseline, `Phase end through next Work Order → phase end to next phase start`.
- **measured** — First doc to first phase totals 1250.27 h over 45 plans; median 451.1 s, p90 38.27 h. The largest listed plan takes 710.20 h (n=1). These spans include time before the window and can include idle time. Lead time is not recorded for 54 plans. First commits are not recorded for 55 of 120 documents, so the measured set is incomplete. Sources: Baseline, `Doc pipeline summary → first doc to first phase`; `Doc pipeline: top 10 plans by lead time → tool-based-ui-startup-polish.md`; document-commit coverage note.
- **measured** — Implementation takes 20.7% of phase wall time: 774.00 h over 1373 recorded phases; median 446.7 s, p90 1.23 h. This is the largest measured worker step. Source: Baseline, `Phase steps → pass: implementation`.
- **measured** — Test passes take 6.5% of phase wall time: 241.81 h over 506 recorded phases; median 406.9 s, p90 1.17 h. Source: Baseline, `Phase steps → pass: test`.
- **measured** — Verification takes 4.2% of phase wall time: 156.75 h over 236 recorded phases; median 627.1 s, p90 1.67 h. The overlap rule shares elapsed time with a worker pass when both cover it. Source: Baseline, `Phase steps → activity: Verification`.
- **measured** — Review passes take 2.3% of phase wall time: 84.78 h over 647 recorded phases; median 303.3 s, p90 1103.6 s. Director review without a pass or activity record can fall in uncovered time. Source: Baseline, `Phase steps → pass: review`.
- **measured** — Shrink takes 23.89 h over 297 recorded phases; median 77.7 s, p90 228.7 s, 0.6% of phase wall time. Shrink outside a recorded phase is not in this row. Source: Baseline, `Phase steps → activity: shrink`.
- **measured** — UX review takes 21.12 h over 107 recorded phases; median 368.0 s, p90 1889.8 s, 0.6% of phase wall time. Source: Baseline, `Phase steps → activity: UX review`.
- **measured** — The 41 small activity labels together take 13.87 h over 76 recorded phases; median 187.1 s, p90 1735.2 s, 0.4% of phase wall time. This summary cannot assign their cost to each named step. Source: Baseline, `Phase steps → activity: other (41 labels)`.
- **measured** — One Mac smoke activity takes 11.77 h (n=1; median and p90 11.77 h), 0.3% of phase wall time. It is in natedev's records; the script reads no Mac records. Source: Baseline, `Phase steps → activity: Mac smoke`.
- **measured** — Smoke takes 4.95 h over 28 recorded phases; median 53.4 s, p90 3361.3 s, 0.1% of phase wall time. Source: Baseline, `Phase steps → activity: smoke`.
- **measured** — Director input totals 29,039,391,216 tokens over 1454 recorded phases; median 6,473,348 (about 6.5 M), p90 44,975,747 (about 45 M). Input includes caches, and shared sessions can overlap. Over 1417 phases with pass bounds, median input-plus-output share is 40.2% after the last pass and 11.1% before the first pass. These shares do not isolate compose before phase start. Sources: Baseline, `Director tokens summary → input per phase`, `after last pass share`, and `before first pass share`.
- **measured** — The contract requires at least 4252 lines, 239441 bytes (about 239 KB), across 13 files per phase, with one read of each dispatch file. `direct.md` alone has 1579 lines and 92400 bytes (n=1 file). More dispatches add reads. This is static required load, not proof of past reads. Sources: Baseline, `Contract full reads → sum per phase (minimum)` and `commands/unit/direct.md`.
- **measured** — Phases inserted after run start total 545 over 37 plans; median 5 per plan, p90 53. Churn follows each plan from its first run through the window end, so some edits can precede the window. Sources: Baseline, `Plan churn summary → phases inserted`; churn-window note.

- **reported** — One run record can hold more than one plan phase. Recorded event boundaries cannot yet give the final phase meaning or fix cost per plan phase; fix-rounds-unit owns both in its unmerged `scripts/history/rounds.py`. Source: fix-rounds-unit via the sdlc showrunner, relayed by the unit director, 2026-10-20.
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

Which goal should decide between a change that cuts director token use and one that cuts time to dispatch? Recommend cutting director token use first while keeping judgment quality and measuring both goals; the 40.2% median share after the last pass is a useful place to study. Sources: Baseline, `Director tokens summary → after last pass share`; `Doc pipeline summary → first doc to first phase`; `docs/plans/sdlc-phases.md`, Phase 2; `M/director-model-quality.md`.
