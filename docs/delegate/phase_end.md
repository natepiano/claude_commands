# Phase end

Read at the point of use from `/unit:direct`. Defines `<PhaseEnd/>` in full.

**Read when:** a loop or verbose phase's gates pass. `single` keeps its order in
`commands/unit/direct.md` and never commits.

Pointing files: `commands/unit/direct.md`, `commands/unit/checkpoint.md`,
`docs/delegate/run_phase_review.md`, `docs/production_format.md`,
`commands/unit/add_ons.md`, `commands/plan/shrink.md`, and
`commands/plan/phase_review.md`.

<PhaseEnd>
Run these steps in order for each completed loop or verbose phase:

1. **Code commit.** Run <CheckpointCommit/> for the phase's code, tests, and
   plan doc with `status: done` and its Work Order still in place. Its
   reservation release, <PushCheckpoint/>, and `review-trial` line happen here.
2. **Code ready notice.** Send <ProductionUnit/> item 3's checkpoint notice.
   Its hash is mergeable now; `Phase <next> ETA` counts from this commit.
   Outside a production, report
   `Checkpoint <short hash> — phase N: <title>.`.
3. **Plan review.** Run <RunPhaseReview/> before the next phase's Work Order is
   used. A defect found in the committed phase becomes a follow-up phase
   inserted next under `/plan:to_phased_plan` → <PhaseNumbering/> and runs next.
   A source-comment edit made by the review stays in the tree for the next
   phase's checkpoint commit; with no todo phase left, <FinalGateCommit/>
   carries it.
4. **Add-on check.** Run <ConsiderNextItems/>.
5. **Worker cleanup and completion.** Run the worker half of <PhaseCleanup/>:
   `remove_seats.py`, helpers, launchers, and apps. Run it before the next
   launch. Per <LongLivedSeats/>, Codex turns finish but their threads remain
   open until `end_session.sh`; Claude seats keep their existing cleanup. Then
   run <RecordPhaseCompletion/>, including `finish-phase` and deletion of the
   inactive reservation state file before the next phase writes its own.
6. **Next launch.** In loop mode or an active auto window with a next phase,
   run <NextPhase/> through that phase's <LaunchImplementation/>. The review has
   settled its Work Order; the phase's seats do not edit the plan doc.
7. **Shrink beside the seats.** Open a `progress_history.py` activity labelled
   `shrink`. In an enrolled repository, make the shrink's first plan-doc
   write with the Edit tool, so the edit hook claims the plan doc for the
   running phase's reservation; a write from a script is never claimed. Run
   <RunPhaseShrink/> on the completed phase, then <ShrinkCommit/>,
   then <ProductionUnit/> item 3's shrink notice when applicable. Run the
   review-prose half of <PhaseCleanup/> (`clear_phase_review.sh`), close the
   activity with `--status completed`, and, when seats are running, end the
   turn holding on them. <DispatchContract/> allows this synchronous work after
   launch besides the main review.

When no todo phase remains, run step 7, then <NextPhase/> for <FinalGate/>.
When a <PeriodicCI/> point is due (outside a production), run step 7 before
step 6, so CI runs on a clean tree; then the CI point, then step 6.
When a todo phase remains but verbose mode is outside an auto window, run step
7 without a launch, then
<VerbosePostPhaseReport/> and <VerbosePostPhaseGate/>. Nothing starts before
`continue` or a new auto control.

A failed structural check in <RunPhaseShrink/> blocks only <ShrinkCommit/>.
Close the activity with `--status error`, keep the seats running and the review
prose intact, and repair the shrink before the next checkpoint. That checkpoint
refuses an earlier `done` phase with a Work Order. The shrink owns no
reservation and never releases the next phase's.

A compaction handoff written while the shrink, its commit, or its notice is
pending names the completed phase.
</PhaseEnd>
