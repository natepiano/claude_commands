# Delegate — checkpoint

**Usage:** `/plan:delegate_checkpoint`

Type this when a phase finished and was never committed, or when a run stopped
between the commit and the reservation release. It runs inside the current
session and already knows the phase, the plan doc, and the mode. If no delegate
run is active, say so in one line and stop.

This is durable state with no cheap undo. Read the whole contract before acting
on any part of it, and read the reservation record from disk — a value
remembered from conversation, from the harness session mapping, or re-derived
from current `HEAD` is not proof and will silently accept the wrong checkpoint.

`/plan:delegate` reads this file once per completed phase in loop and verbose
mode. It defines `<CheckpointCommit/>` in full. `single` never commits.

Everything below is the contract.

---

<CheckpointCommit>
Loop/verbose only:

1. Require smoke pass, `not applicable`, or `deferred`. Style is not a phase
   gate: <RunProjectStyleReview/> runs once at <FinalGate/>, over every
   checkpoint this one joins.
2. Confirm status contains only this phase, its plan doc, and an approved change
   to `${NEXT_ITEMS_PATH}` when present.
3. Run `verify.sh fmt <package>` for every touched package; include resulting
   formatting changes.
4. Mark the phase `status: done`. Never put its commit hash in the plan.
5. Read and validate
   `${SESSION_DIR}/delegated_phase_reservation_state.json`; never use a value
   remembered from conversation or the harness-session mapping.

   | Durable state | Action |
   | --- | --- |
   | `Active`, phase current | Run the full phase-start drift comparison below exactly once. |
   | `RepositoryNotEnrolled`, `EnrolledAwaitingFirstTouch` | Skip drift; neither owns a reservation. |
   | `CheckpointCommittedAwaitingReleaseConfirmation` | Valid only as a resumed state: route directly to step 7, re-entering neither drift nor checkpoint creation. |

   ```sh
   PYTHONPATH="$HOME/.claude/scripts" python3 -m berth.claim_state invoke \
     --cwd "${WORKING_DIR}" --expected-verb drift -- drift --full --json
   ```

   Require a validated exit-`0` result with `payload.kind = drift`,
   `payload.data.comparison = full_phase_start`, and exactly one
   `payload.data.results` entry whose `reservation_id` equals the
   `reservation_id` in the durable `ActivePhaseReservation`. Bind that result to
   the record's `phase_start_head`, because `full_phase_start` means the engine
   compared the selected reservation against its protected phase-start baseline.
   The response does not echo that object id, so do not invent an OID field or
   re-derive acting identity. The cheap drift delta is forbidden here.

   Anything else is identity ambiguity or unsafe drift and blocks the
   checkpoint: apply <RetainDelegatedPhaseReservation/> and report the exact
   full-drift command above so a person can run it to see the conflict. That
   covers no result, more than one, a different reservation id, any comparison
   other than `full_phase_start`, an incursion, collision, attribution
   requirement, absent or corrupt ledger, a malformed response, and the
   `Unidentified` identity case — an exit-`5` `invalid_input` whose diagnostic
   says drift requires a live session mapping, active coordination-run marker,
   or `CARGO_BERTH_RUN`. Exit `6` has already spent the engine's single
   ten-second deadline: invoke no retry.
6. Stage the phase, its plan doc, and `${NEXT_ITEMS_PATH}` when approved and
   changed; commit exactly once:

   ```
   checkpoint(<plan-slug>): phase N — <title>

   <what the phase built>

   Claude-Session: <session url>
   ```

   A failed commit is an unsuccessful ending: do not invoke release, leave
   `Active` untouched, and apply <RetainDelegatedPhaseReservation/>. After a
   successful commit from `Active`, immediately capture the full output of
   `git rev-parse HEAD`, atomically replace the `Active` record with
   `CheckpointCommittedAwaitingReleaseConfirmation` carrying that value as
   `checkpoint_commit`, and read the new record back. That durable transition
   must succeed before release is invoked, and its value comes only from the
   commit that just succeeded, never from conversation or from a later reading
   of `HEAD`. If capture, atomic replacement, or read-back fails, do not invoke
   release and apply <RetainDelegatedPhaseReservation/> to whichever valid
   durable state remains.
7. Read and validate the durable record again.

   | State | Action |
   | --- | --- |
   | `CheckpointCommittedAwaitingReleaseConfirmation` | Read its reservation id and `checkpoint_commit` from the read-back `CheckpointReleaseConfirmationPending`, then invoke `/sync release` exactly once. |
   | `Active` | Step 6's durable transition did not complete: do not invoke release; apply <RetainDelegatedPhaseReservation/>. |
   | Either inactive state | Invoke no release. |

   ```sh
   PYTHONPATH="$HOME/.claude/scripts" python3 -m berth.claim_state invoke \
     --cwd "${WORKING_DIR}" --expected-verb release -- \
     release <recorded-reservation-id> --json
   ```

   Do not pass a commit: `release` snapshots the invoking worktree's current
   HEAD. **First-attempt assertions**, not weakened by recovery: exit `0`,
   envelope status `outstanding`, release payload status `checkpointed`, the
   requested reservation id, and `payload.data.protected_tip` equal to the
   durable record's read-back `checkpoint_commit`. Also require its
   `payload.data.session_mapping_publication`; report its `unavailable`
   diagnostic without treating the journalled checkpoint as absent.

   A resumed session obtains `checkpoint_commit` by reading the durable record,
   never by re-deriving it from current `HEAD`. `HEAD` is not proof: a later
   commit in the same worktree would silently supply the wrong answer and make
   the comparison accept the wrong checkpoint.

   **Recovery.** A process kill, crash, or power loss can retain the durable
   record after the `Checkpoint` operation was appended but before this workflow
   observed the reply. On a later recovery from that retained record, invoke the
   same release once. An exit-`0` reply naming the requested reservation with
   payload status `resnapshotted`, `evidence_revalidated`, or `released` instead
   of `checkpointed` requires the lifecycle query below. Do not gate recovery on
   envelope status: `resnapshotted` has `outstanding`, while
   `evidence_revalidated` can legitimately have `outstanding`, `integrated`,
   `trunk_rewritten`, or `object_unknown` because that status reports current
   integration evidence, not reservation lifecycle — evidence replay preserves
   the `outstanding` lifecycle and its original protected tip. Only the
   lifecycle query establishes protected-tip equality with the journalled
   checkpoint.

   ```sh
   PYTHONPATH="$HOME/.claude/scripts" python3 -m berth.claim_state reservation \
     --cwd "${WORKING_DIR}" --reservation <recorded-reservation-id>
   ```

   Inspect the validated coordinator `state`, which is derived from
   `envelope.payload.data` without reading `message`. Exit `0` requires
   `kind = reservation_lifecycle`, the requested `reservation_id`, and exactly
   one lifecycle alternative: `active`; `outstanding` with `protected_tip`;
   `released_after_checkpoint` with `protected_tip` and `disposition`; or
   `released_without_checkpoint` with `disposition`. Exit `5` is valid only for
   `kind = unknown_reservation` carrying the requested `reservation_id`.

   | Lifecycle result | Disposition |
   | --- | --- |
   | `outstanding`, `protected_tip` equal to the read-back `checkpoint_commit` | The same checkpoint release is already journalled; confirm it. |
   | `released_after_checkpoint` with that same protected tip | Confirm that release, then report it as recovered and subsequently released with the reported terminal disposition. |
   | A different protected tip | Another release; retain the record. |
   | `active`, `released_without_checkpoint`, `unknown_reservation`, a mismatched echoed id, or a busy, unreadable, malformed, or otherwise invalid response | Retain the record with that distinct reason. |

   Do not consult the retention ref: it proves commit reachability but not
   whether the selected reservation is outstanding or released. Report a
   matching checkpoint as recovered rather than re-made. Only after the
   first-attempt structured assertions or these recovery assertions pass, delete
   `${SESSION_DIR}/delegated_phase_reservation_state.json`.

   An ordinary failed release did not append anything because transaction
   validation precedes the journal write; it is not recovery, and the original
   first-attempt assertions still apply when it is run again. At the moment of
   any busy or failed release, invoke no retry and apply
   <RetainDelegatedPhaseReservation/>. The checkpoint commit exists, but the
   phase does not complete until a later invocation confirms either the normal
   first-attempt reply or the matching already-journalled checkpoint above.
8. Report `Checkpoint <short hash> — phase N: <title>.` Never push here. This
   report follows successful release when the phase was active.
</CheckpointCommit>
