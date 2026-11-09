# Early review

<EarlyReviewArm>
The reviewer reads the partial change while the implementation worker continues.
The hook submits Stage 2 after the director publishes the final diff.

Arm one adversary review only when all conditions hold:

- An implementation or fix dispatch is active.
- `EARLY_REVIEW=none`.
- The completed dispatch requires <DualReview/> or <ClosureReview/>.
- The worker has completed at least 75% of its pass.
- At least ten minutes of work remain.

Use the worker's report, heartbeat, and diff as evidence.
Do not arm a behavior-preserving repair or a repair with a narrow path set.
Launch the contract review after completion under <DualReview/>.

1. Set `REVIEW_PASS` to the next review index.
2. Remove earlier final diffs and ready sentinels from `${PHASE_DIR}`.

   `rm -f "${PHASE_DIR}"/final_diff_*.diff "${PHASE_DIR}"/final_diff_*.ready`

3. Apply <ReviewDiffContract/> to the partial change.
4. Select the unit reviewer under <TeamReview/>.
5. Save its worker name as `${EARLY_REVIEW_SLOT}`.
6. Write the early prompt under <BroadReviewPrompt/> or <ClosureReview/>.
7. Include the estimate, partial diff, final diff path, and ready sentinel path in the prompt.
8. Submit the review under <DispatchContract/>.

   `assign.sh --role review --to "${EARLY_REVIEW_SLOT}" --lens adversary --pass "${REVIEW_PASS}" --phase "<phase id>" --session-dir "${SESSION_DIR}" --cwd "${WORKING_DIR}" --prompt <early prompt> --early-ready "${PHASE_DIR}/final_diff_${REVIEW_PASS}.ready"`

9. Set `EARLY_REVIEW=launched`.
10. End the turn after submission.

The launcher records the pass and returns after submission.
The hook keeps the pass active between Stage 1 and Stage 2.
No launcher watches the sentinel.

After the implementation completes:

1. Write the final diff to `${PHASE_DIR}/final_diff_${REVIEW_PASS}.diff` under <ReviewDiffContract/>.
2. Limit a closure review to its paths under <ClosureReview/>.
3. Create `${PHASE_DIR}/final_diff_${REVIEW_PASS}.ready` after the diff is fully written.
4. Send the ready event for the reviewer.

   `~/.claude/scripts/lib/py ~/.claude/scripts/herdr/turn_end.py stage-ready "${EARLY_REVIEW_SLOT}"`

5. End the turn until the hook sends the review result.

The ready event and the Stage 1 hook use the same pass.
Either event can arrive first.
The hook submits Stage 2 once both the Stage 1 reply and the sentinel exist.

| Event | Required action |
| --- | --- |
| The implementation fails or stops | End the reviewer under <PassOwnership/>. Cancel its active pass. Clear `EARLY_REVIEW` and `${EARLY_REVIEW_SLOT}`. |
| The reviewer fails before delivery | Report the failure. Clear `EARLY_REVIEW` and `${EARLY_REVIEW_SLOT}`. Start the next review index after implementation completes. |
| A verdict arrives before the final diff | Discard the verdict. Record no finding from it. Follow the reviewer failure action. |

Keep the numbered artifacts when an early review fails.
</EarlyReviewArm>
