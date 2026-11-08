# Early review

<EarlyReviewArm>
Launch the blind reviewer while the writer is still running, so both finish
together instead of back to back. Evaluate only at a Codex poll timeout or a
requested <ProgressReport/>, and arm only when all of these hold: an implementation or fix dispatch is
active; `EARLY_REVIEW=none`; the completed dispatch would receive a worker
review — <DualReview/> pass 1 or a closure review; **pass-internal** completion
of that dispatch is at least 75%; and at least ten minutes of writer time still
remain.

Read completion from that report's or poll's evidence — the heartbeat shows the worker
running its listed verification commands, or the diff already covers essentially
all Work Order (or fix batch) files — and the remainder from how long the pass
has already run against that estimate. Ten minutes left at 75% means a pass of
roughly forty minutes or longer, so most implementations and nearly every repair
arm nothing and review synchronously; that is the intended outcome, not a missed
opportunity. A behavior-preserving repair never arms — documentation,
formatting, lint guidance, an agreed trivial rename — and neither does one whose
batch sits in paths narrow enough that <FixDispatch/> will close it on a
contained diff; that judgment is the unit director's own reading of the batch, as
no task name carries it. When the two estimates disagree, or either rests on no
evidence, do nothing; the synchronous path still exists.

At an eligible point, in that same turn:

1. Increment `${REVIEW_PASS}` now; <DualReview/> will not increment it again.
2. **Delete every stale delivery artifact and prove they are gone.**
   A resumed or repeated phase reuses `${PHASE_DIR}`, so an earlier delivery's
   artifacts can already sit on disk under the exact names this launch will
   poll.

   `rm -f "${PHASE_DIR}"/final_diff_*.diff "${PHASE_DIR}"/final_diff_*.ready`

   Clear the whole glob, not just the current index — later passes in this phase
   collide the same way — then confirm no sentinel remains before continuing. A
   stale sentinel releases the reviewer onto a previous phase's diff, which
   returns a confident and entirely false blocker saying the phase implemented
   nothing; and because the sentinel is what releases `assign.sh` to call
   `start-pass`, it opens the review pass while the writer is still running, so
   the recorder closes the live implementation pass as `interrupted` and refuses
   every later `progress` call in that phase. Both failures are silent until the
   false verdict or the refused call.
3. Apply <ReviewDiffContract/> to the current partial tree. The worker has
   named no created files yet, so that check is vacuous; the snapshot is
   expected to be incomplete.
4. Use the unit's own reviewer under <TeamReview/>. Save its slot as
   `${EARLY_REVIEW_SLOT}` from the dispatch result.
   Write the applicable early-form prompt — the `adversary`
   lens of
   <BroadReviewPrompt/> for pass 1, <ClosureReview/> for a fix — including the
   completion estimate, the partial diff, and the exact final-diff and
   ready-sentinel paths below. **Only the adversary arms early.** It is the lens
   that gains most from the extra time, and arming both against a partial
   tree would double the exposure to the void verdict below; `contract`
   launches at completion under <DualReview/> step 3.
5. Launch `assign.sh --role review --lens adversary --pass "${REVIEW_PASS}"
   --phase "<phase id>" --session-dir "${SESSION_DIR}" --cwd "${WORKING_DIR}"
   --prompt <early prompt> --early-ready "${PHASE_DIR}/final_diff_${REVIEW_PASS}.ready"`
   under <DispatchContract/>. Save the handle as `${REVIEW_DISPATCH_HANDLE}`
   and set `EARLY_REVIEW=launched`. Leave `${DISPATCH_HANDLE}` untouched.
6. Before the recorder call in <ProgressReport/> step 5, put the reviewer in
   the round table:

   `~/.claude/scripts/lib/py ~/.claude/scripts/direct/progress_history.py arm-review --session-dir "${SESSION_DIR}" --worker "<review worker name>" --phase-id "<phase id>" --activity "<what this reviewer is checking>" --called-task direct.review [--lens adversary]`

   Pass the same lens and phase the launch carries, and no lens where the
   launch carries none: the marker names the status and pid files it will watch for, and a
   marker watching the unsuffixed pair while the launcher writes the suffixed
   one retires the row on its first tick, reporting a live reviewer as gone.
   It is a presentation marker, not a pass event, so it cannot forge anything
   convergence counts. It resolves the same reviewer `assign.sh` will, retires
   its row when `assign.sh` errors or its process is gone, and is superseded
   when the sentinel lets `assign.sh` open the real pass. The two running rows
   are the announcement, and they carry what a sentence cannot: which agent each
   one is, when it started, and how long it has been going.

The extra argument defers `assign.sh`'s `start-pass` until the sentinel appears,
so the implementation pass and the review pass never overlap in the recorder. At
most one early launch per dispatch; otherwise review runs synchronously, as it
always may — early launch is opportunistic, never required.

| Event | Required action |
| --- | --- |
| Primary completes | After <LaunchImplementation/> step 7, write the final diff to `${PHASE_DIR}/final_diff_${REVIEW_PASS}.diff` — a closure review stays limited to its paths per <ClosureReview/> — then create `${PHASE_DIR}/final_diff_${REVIEW_PASS}.ready`. **Never create the sentinel before the diff is fully written.** `assign.sh`'s run then sends the reviewer its Stage 2. |
| Primary errors, or the run stops | Kill the early reviewer. If the sentinel exists, close its pass with `UNIT_TEAM_ROLE=${EARLY_REVIEW_SLOT} … finish-pass --status canceled --orphaned-launcher` per <PassOwnership/>; skip this call if the launcher opened no pass. Before the sentinel no pass was recorded, so record nothing — a pre-sentinel kill counts toward no advisory, including the blind-review cancellation one. |
| Reviewer errors before delivery | Report it in one line, clear `${REVIEW_DISPATCH_HANDLE}` and `${EARLY_REVIEW_SLOT}`, set `EARLY_REVIEW=none`, and leave the numbered artifacts. The primary continues and is reviewed synchronously at completion under the next `${REVIEW_PASS}` index. |
| A verdict arrives before delivery | **It is void.** The reviewer cannot have read a diff that does not exist yet, so discard its findings entirely rather than reading them as evidence: open nothing in the ledger, preempt nothing, route no blocker into a fix dispatch. Say in one line that it is discarded and why, then follow the reviewer-error row. A void verdict is often fluent and specific — a stale diff supports confident claims about missing work — so the check is the timing, never how convincing the text reads. |

Every path that ends an early launch before its real pass starts also drops the
row it was given:

`~/.claude/scripts/lib/py ~/.claude/scripts/direct/progress_history.py disarm-review --session-dir "${SESSION_DIR}" --reason "<canceled|reviewer error|void verdict>"`

Run it alongside clearing `${REVIEW_DISPATCH_HANDLE}`, `${EARLY_REVIEW_SLOT}`,
and `EARLY_REVIEW`. The
row would retire itself at the next report anyway; this is how the reason
reaches the record, and it is what lets that report state the reason rather
than infer one.
</EarlyReviewArm>
