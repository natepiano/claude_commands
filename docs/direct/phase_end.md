# Phase end

Read at the point of use from `/unit:direct`. Defines `<PhaseEnd/>` in full.

**Read when:** a loop or verbose phase's gates pass. `single` keeps its order in
`commands/unit/direct.md` and never commits.

Pointing files: `commands/unit/direct.md`, `commands/unit/checkpoint.md`,
`docs/direct/run_phase_review.md`, `docs/production_format.md`,
`commands/unit/add_ons.md`, `commands/plan/shrink.md`, and
`commands/plan/phase_review.md`.

<PhaseEnd>
Standing plans (`**Standing:** style_plan.py`) run <StandingPlanRefill/> at the next boundary, even with no todo phase.
Read `docs/direct/standing_plan_refill.md` for that rule. Skip step 7 (shrink,
shrink commit and notice); never stage, commit or shrink the excluded plan or
pinned rule files. A no-change ticket follows <CheckpointCommit/> step 6's
ancestry-checked HEAD path with no code commit, push or merge request, then
refills; other tickets checkpoint the code commit before marking the phase done.

Run these steps in order for each completed loop or verbose phase:

1. **Code commit.** Run <CheckpointCommit/> for the phase's code, tests, and
   plan doc with `status: done` and its Work Order still in place. Its
   reservation release, <PushCheckpoint/>, and `review-trial` line happen here.
2. **Code ready notice.** Before sending <ProductionUnit/> item 3's checkpoint
   notice, run
   `~/.claude/scripts/lib/py ~/.claude/scripts/direct/progress_history.py eta --session-dir <SESSION_DIR> --phase-id <next> --time <HH:MM> --basis checkpoint`.
   `<HH:MM>` means today in the user's zone; an ETA on a later day takes
   `<YYYY-MM-DDTHH:MM>`. State the time the recorder printed. Its hash is
   mergeable now;
   `Phase <next> ETA` counts from this commit.
   Read `findings.py status --phase <completed phase>` before sending the notice.
   Include every `held_for_decision` item as `Held simplify: <id> — <files> — <proposed change>; ledger <path>`.
   Outside a production, report
   `Checkpoint <short hash> — phase N: <title>.`.
   Start the daily simplify run as a background shell after the code ready notice:

   ```sh
   bash ~/.claude/scripts/direct/assign.sh --role simplify --session-dir "${SESSION_DIR}" --cwd "${WORKING_DIR}"
   ```

   The next phase does not wait for this run.
3. **Plan review.** Run <RunPhaseReview/> before the next phase's Work Order is
   used. A defect found in the committed phase becomes a follow-up phase
   inserted next under `/plan:to_phased_plan` → <PhaseNumbering/> and runs next.
   A source-comment edit made by the review stays in the tree for the next
   phase's checkpoint commit; with no todo phase left, <FinalGateCommit/>
   carries it. The review launches with `--phase <id>`, which needs the phase
   open: its `finish-phase` in step 5 waits until this review has finished,
   also beside a running phase.
4. **Add-on check.** Run <ConsiderNextItems/>.
5. **Worker cleanup and completion.** Run the worker half of <PhaseCleanup/> before the next launch.
   Retain pane workers and their native conversations under <LongLivedWorkers/>.
   Then run <RecordPhaseCompletion/>, including `finish-phase` and deletion of the
   inactive reservation state file before the next phase writes its own.
6. **Next launch.** In loop mode or an active auto window with a next phase,
   run <NextPhase/> through that phase's <LaunchImplementation/>. The review has
   settled its Work Order; the phase's workers do not edit the plan doc.
7. **Shrink beside the workers.** Open a `progress_history.py` activity labelled
   `shrink`. In an enrolled repository, make the shrink's first plan-doc
   write with the Edit tool, so the edit hook claims the plan doc for the
   running phase's reservation; a write from a script is never claimed. Run
   <RunPhaseShrink/> on the completed phase, then <ShrinkCommit/>,
   then <ProductionUnit/> item 3's shrink notice when applicable. Run the
   review-prose half of <PhaseCleanup/> (`clear_phase_review.sh`), close the
   activity with `--status completed`, and, when workers are running, end the
   turn holding on them. <DispatchContract/> allows this synchronous work after
   launch besides the main review.

When no todo phase remains, run step 7, then <NextPhase/> for <FinalGate/> in `docs/direct/final_gate_commit.md`.
When a <PeriodicCI/> point is due (outside a production), run step 7 before
step 6, so CI runs on a clean tree; then the CI point, then step 6.
When a todo phase remains but verbose mode is outside an auto window, run step
7 without a launch, then
<VerbosePostPhaseReport/> and <VerbosePostPhaseGate/>. Nothing starts before
`continue` or a new auto control.

A failed structural check in <RunPhaseShrink/> blocks only <ShrinkCommit/>.
Close the activity with `--status error`, keep the workers running and the review
prose intact, and repair the shrink before the next checkpoint. That checkpoint
refuses an earlier `done` phase with a Work Order. The shrink owns no
reservation and never releases the next phase's.

A compaction handoff written while the shrink, its commit, or its notice is
pending names the completed phase.
</PhaseEnd>
