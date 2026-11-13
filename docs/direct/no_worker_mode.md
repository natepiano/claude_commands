# Work without workers

<NoWorkerMode>
Complete the current work directly.
This exception covers implementation, repairs, code review, style review, and phase review.
Keep the normal scope, authorization, reservation, verification, and checkpoint gates.
Use the existing run.
Apply <PrepareSession/> when no run remains.

The no-capacity broadcast stops every Codex worker on the machine; you stop none yourself.
`worker_capacity.py` sends you one stop report when it changed a worker of yours.
- `Stopped` names each worker whose process it stopped.
- `Passes ended as canceled` names each worker whose open pass it ended. Continue that work from the worktree as a director activity; do not redispatch the pass.
- `Pass stays open` names a pass that it could not end, with the cause. Tell your showrunner.
- `Detached run continues` names a run that is not stopped, with its result file and its log. Its result comes to you when the run ends; it starts no worker.
- `Not proved stopped` names a worker that can still run. Send it no work; your showrunner gets the same fault.
Claude workers are not stopped. The workers-back notice starts no worker; your next assign starts it.

Use `PY=~/.claude/scripts/lib/py` and `DIRECT=~/.claude/scripts/direct` in the commands below.
Run them from `${WORKING_DIR}` with the normal phase ID and paths.

1. Open the phase before editing:

   ```sh
   "$PY" "$DIRECT/progress_history.py" start-phase --session-dir "${SESSION_DIR}" --phase-id "<id>" --phase-title "<title>" --work-order-file "${PHASE_DIR}/implementation_prompt.md"
   "$PY" "$DIRECT/phase_reservation.py" claim --session-dir "${SESSION_DIR}" --phase "<id>" --repo "${WORKING_DIR}"
   ```

   Keep the Work Order and authorized paths in `${PHASE_DIR}/implementation_prompt.md` and `${PHASE_DIR}/files` before the claim.
   `claim` records `phase_baseline` and `progress_baseline_status` without a worker pass.
   Retain any baseline from an earlier launch of this phase.
2. Record your work as a director activity:

   ```sh
   "$PY" "$DIRECT/progress_history.py" start-activity --session-dir "${SESSION_DIR}" --label "Direct implementation" --activity "Implement phase <id> without workers"
   ```

   Write the code and run the Work Order's verification under <VerificationContract/>.
   Finish the activity with `progress_history.py finish-activity --session-dir "${SESSION_DIR}" --status completed --result "<actual result>"`.
   Use `error` for a failed activity.
   Never call `start-pass` or `finish-pass` to represent direct work.
   Those commands require a launcher; direct activity does not create a worker pass.
3. Prepare the normal <ReviewPromptContract/> inputs from the phase baseline and current tree.
   Read `config/review/code.md` and the original Work Specification.
   Apply the `adversary` and `contract` lenses yourself in separate reads.
   Include the `rerun` lens when <DualReview/> requires it.
   Save each review result under `${PHASE_DIR}` and record review activities with `start-activity` and `finish-activity`.
   These are director reviews, with no claim of independent worker review.
4. Record findings through <FindingsLedger/> and run its existing commands:

   ```sh
   "$PY" "$DIRECT/findings.py" gate --session-dir "${SESSION_DIR}" --phase "<id>"
   "$PY" "$DIRECT/findings.py" dispatch --session-dir "${SESSION_DIR}" --phase "<id>" --covers "<complete batch>" --accepted-checks "<checks or none: reason>"
   ```

   Use `dispatch` only for the gate's repair batch.
   This command records the batch without launching a worker.
   Keep its existing routing, Astra, and accepted-check requirements.
   Repair the batch yourself, then record `findings.py landed --session-dir "${SESSION_DIR}" --phase "<id>"`.
   Perform closure review before `findings.py verdict --session-dir "${SESSION_DIR}" --phase "<id>" --id "<finding>" --state accepted --evidence "<repair and checks>"`.
   Repeat the gate until it reports `converged`.
   The gate accepts zero worker passes.
5. Complete owed style review through the manual <StyleReview/> entry point:

   ```sh
   "$PY" ~/.claude/scripts/lint/style_review.py prepare --working-dir "${WORKING_DIR}" --since "<phase_baseline>" --phase-dir "${PHASE_DIR}" --prompt "<empty prompt outside the repository>" --directory-file "${PHASE_DIR}/style-input-directory"
   ```

   Read the returned input directory's `prompt.md` and apply its assignment yourself.
   Save the marked result outside the repository.
   Capture it with `style_review.py finish --input-dir "<returned directory>" --result "<saved result>" --dispatch-code 0`.
   Preparation records `Off` and `No changes to review` itself.
   This command accepts a director process and needs no worker pass.
6. Complete smoke, UX, phase review, and shrink under their existing contracts.
   Perform reviews directly where those contracts normally dispatch.
   Apply <CheckpointCommit/> after the required checks and converged findings.
   Run `phase_reservation.py record-active --session-dir "${SESSION_DIR}" --phase "<id>"` before its reservation checks.
   Stage the final tree and run `verify.sh doc` immediately before each Cargo checkpoint.
   Record the successful commit with `progress_history.py checkpoint --session-dir "${SESSION_DIR}" --phase "<id>" --commit "<full hash>"`.
   This command requires the open phase and commit hash, with no worker pass requirement.
   Complete reservation release and push through <CheckpointCommit/>.
7. Finish the phase with `progress_history.py finish-phase --session-dir "${SESSION_DIR}" --phase-id "<id>" --status completed`.
   Keep broadcast-on mode through later phases until the workers-back notice arrives.
   Clear self-on mode at the next phase start.
   Try a normal assign.
</NoWorkerMode>
