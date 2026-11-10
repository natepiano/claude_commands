# Launch implementation

Read at the point of use from `/unit:direct`. Defines `<LaunchImplementation/>`
in full.

**Read when:** before a phase's first dispatch, and again when that dispatch
completes.

<LaunchImplementation>
1. The launcher's `phase_reservation.py claim` writes `${PHASE_DIR}/phase_baseline`
   (the repository's full `HEAD` id) and `${PHASE_DIR}/progress_baseline_status`
   (`git status --short`) at the phase's first launch, before the worker opens.
   A git failure stops the launch. Later launches, including fixes and reopened
   phases, keep both first baselines. Ad hoc work uses `${SESSION_DIR}/phases/adhoc`.
2. Close the outgoing phase before opening this one, per <PassOwnership/>: when a
   finished phase's record is still active, run `progress_history.py finish-phase
   --session-dir "${SESSION_DIR}" --status completed` first. A phase opened
   beside one still running leaves that one open. Then run
   `progress_history.py start-phase --session-dir "${SESSION_DIR}"
   --phase-id <id> --phase-title <title> --work-order-file
   "${PHASE_DIR}/implementation_prompt.md"`. Use `ad hoc` plus scope without a
   phased plan; pass the original prompt only. Both run before the dispatch in
   step 4, never after it.
3. `~/.claude/config/models.conf` owns worker family/model/effort, one row
   per kind. On a worker's first phase, its kind is its opening role from the Work
   Order's `Workers:` field: `impl` for the `impl` slot, and for `test` whatever
   its Workers line opens it as — `test` under the default opening. On later
   phases, task and kind name the role assigned by that phase's Workers field.
   State the assignment in the dispatch update in ordinary words: "opening 2
   writers: …" on the first phase, "continuing 2 writers: …" later.
4. Take the partition and the opening from Workers and write one prompt per slot
   under <WritePromptContract/>: `${PHASE_DIR}/implementation_prompt.md` for
   `impl` and `${PHASE_DIR}/test_prompt.md` for `test`. Only when the field is absent, partition per <TeamFilePartition/>
   yourself and say so in the dispatch update. Opening and follow-up prompts
   both carry the inline Three Gods sentence and the Type Design Contract or
   its allowed pointer.
5. Apply <LongLivedWorkers/> and launch both workers together, `impl` first, under <ToolingContract/>.
   Inspect `worker.py list`.
   Omit `--to` for automatic selection, or pass a retained worker's short name.
   The worker command reuses idle or stopped workers and retains their native conversations.
   It refuses selection when all suitable workers are busy.

   Use these commands with the selected worker names:

   ```sh
   bash ~/.claude/scripts/direct/assign.sh --role implement --to "<impl worker name>" \
     --phase "<phase id>" --session-dir "${SESSION_DIR}" --cwd "${WORKING_DIR}" \
     --prompt "${PHASE_DIR}/implementation_prompt.md" --paths "${PHASE_DIR}/implementation_paths.txt"
   bash ~/.claude/scripts/direct/assign.sh --role test --to "<test worker name>" \
     --phase "<phase id>" --session-dir "${SESSION_DIR}" --cwd "${WORKING_DIR}" \
     --prompt "${PHASE_DIR}/test_prompt.md" --paths "${PHASE_DIR}/test_paths.txt"
   ```

   Set each `--role` from the Work Order's assignment.
   Each launch carries `--phase "<phase id>"` and keeps its artifacts under `${PHASE_DIR}`.
   Keep the artifact paths. Exit 0 means submission, with the pass still active.
   End the director turn after the assigned synchronous review.
   Each turn hook sends the director the worker result and reply path.
   Each worker records its assigned role as its pass kind.
   Responsibility follows <ProgressContract/>.
6. Announce prompt, board, and heartbeat paths, set `EARLY_REVIEW=none`, then
   apply <DispatchContract/> once for the whole team.
7. Read each hook message and its saved reply when the message resumes the director.
   Complete the phase only after both workers finish.
   Load the implementation summary into `${IMPL_SUMMARY}` and read the other worker's summary.
   Apply <WorkerLaunchFailure/> before reporting a launch error.
   `assign.py` ends a run when its only worker command fails and no pass completed.
   Each recorder slot counts as one worker. A Claude launch must add a `pass_started` event for its slot.
   The director must hold only that run. A launcher signal does not trigger this path.
   Only exit 2 qualifies. Exits 3 through 8 retain the run.
   The launcher calls `progress_history.py finish-run --session-dir <run> --status error`, then `end_session.sh`.
   It keeps the launch exit code and the phase reservation. A failed cleanup child reports its error.
   For other genuine errors, cancel early review under <EarlyReviewArm/> and apply <RetainPhaseReservation/>.
   Report the dispatch log and record `finish-run --status error`.
   Run the end step as one command, without a pipe:

   ```sh
   bash ~/.claude/scripts/direct/end_session.sh
   ```

   Report a non-zero exit and the failed step.
   Stop and emit <RunSummary/> for a multi-phase run.
8. A successful dispatch records worker completion; it does not pass the phase gate.
   Read `${IMPL_SUMMARY}` for commands with running, unread, or absent results.
   Run each such command under <VerificationContract/> before <DualReview/>.
   Review the actual output. Record each failure as a finding.
</LaunchImplementation>
