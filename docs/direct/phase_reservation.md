# Phase reservations

<PhaseReservationContract>
Coordination applies to phased Work Orders in every mode. Ad hoc work skips it.

Persist exactly one tagged `PhaseReservationState` for each open
phase at `${PHASE_DIR}/delegated_phase_reservation_state.json`. A script verb
makes every state change, atomically; never write or delete this file by hand.
Read it back before the next lifecycle action; the initial state must be
durable before dispatch. The verbs:

| Change | Verb |
| --- | --- |
| Initial state from `/sync board` | `phase_reservation.py enroll --session-dir "${SESSION_DIR}" --phase <id> --board <board_ready\|unconfigured>` |
| First touch to `Active` | The Codex worker hook, or `phase_reservation.py record-active` at <CheckpointCommit/> step 5 |
| Lost open phase record | `phase_reservation.py repair --session-dir "${SESSION_DIR}" --phase <id> --repo <repository>` requires a recorder-open phase. It restores `Active` from a live mapped reservation and the phase baseline. Without that mapping, a recorded `Active` reservation in `CHECKPOINT_RELEASED` returns to `enrolled_awaiting_first_touch`. Repair logs and posts the change, then prints `claim`. Other reservation-bearing records stay unchanged. Missing records without a live mapping exit 2 with enroll/claim commands. A finished phase prints `progress_history.py start-phase`. A phase with its own release marker refuses repair. |
| `Active` to pending release | `phase_reservation.py committed --session-dir "${SESSION_DIR}" --phase <id> --commit <checkpoint commit>`; a style ticket adds `--ticket <ticket>`, and the record then also names the ticket and the worktree, so the reservation stays held until that ticket's merge |
| Held reservation released at ticket merge | `style_ledger.py ticket merge` runs `phase_reservation.py release-held --session-dir <run> --phase <id> --ticket <ticket> --merged <style branch tip>`: it releases an `active` reservation from the recorded worktree at the checkpoint, confirms on the board a release whose protected tip is the checkpoint, or a later commit that descends from it and that the merged commit contains, and clears the record as `clear --released` does; a record not held for that ticket is a no-op, and a moved worktree or an unconfirmed release exits 2 and keeps it |
| Pending release confirmed | `phase_reservation.py clear --session-dir "${SESSION_DIR}" --phase <id> --released <checkpoint commit>` leaves `reservation_released` with the commit; the phase leaves edit attribution while its plan review finishes. |
| Inactive state at phase end | `progress_history.py finish-phase --status completed` |
| A finished phase reopened | `progress_history.py start-phase` on that phase id restores its enrolled state from the board answer that `enroll` kept |

Exit 2 means the verb refused the change; it names the state it found and
leaves that state as it is. The domain states and on-disk shapes are:

```json
{"kind":"repository_not_enrolled","phase":"<phase>"}
{"kind":"enrolled_awaiting_first_touch","phase":"<phase>"}
{"kind":"active","active_phase_reservation":{"reservation_id":"<id>","coordination_run_id":"<uuid-v7>","phase":"<phase>","phase_start_head":"<full object id>"}}
{"kind":"checkpoint_committed_awaiting_release_confirmation","checkpoint_release_confirmation_pending":{"reservation_id":"<id>","coordination_run_id":"<uuid-v7>","phase":"<phase>","phase_start_head":"<full object id>","checkpoint_commit":"<full object id>"}}
{"kind":"checkpoint_committed_awaiting_release_confirmation","checkpoint_release_confirmation_pending":{"reservation_id":"<id>","coordination_run_id":"<uuid-v7>","phase":"<phase>","phase_start_head":"<full object id>","checkpoint_commit":"<full object id>","held_for_ticket":"<ticket>","worktree":"<unit worktree>"}}
```

| State | Created only from | Lifecycle |
| --- | --- | --- |
| `RepositoryNotEnrolled` | `/sync board` returning `unconfigured` | Owns no reservation; supplies no release argument. |
| `EnrolledAwaitingFirstTouch` | `/sync board` returning `board_ready` | The registered edit hook still owns first-touch acquisition; supplies no release argument. |
| `Active` | A clear check that acquired or already held the scopes: the Codex worker hook, or the checkpoint backstop for a first touch no hook recorded. A later clear check that names another reservation of the same phase replaces it, as after berth released the recorded one. `repair` may restore it from berth's session map only after the board confirms it is active | Copies the returned reservation and coordination-run ids, phase, and protected phase-start HEAD; supplies a release argument. |
| `CheckpointCommittedAwaitingReleaseConfirmation` | <CheckpointCommit/> step 6, atomically replacing `Active` once that step's commit succeeds | Copies the active fields plus the full object id captured immediately from that commit; supplies a release argument, and resumes only at release confirmation. A style ticket's record also names the ticket and the worktree; it supplies no release argument, and only `release-held` at that ticket's merge releases it. |

These are
`PhaseReservationState::{RepositoryNotEnrolled,
EnrolledAwaitingFirstTouch, Active(ActivePhaseReservation),
CheckpointCommittedAwaitingReleaseConfirmation(
CheckpointReleaseConfirmationPending)}`. Do not represent any of them as an
absent record or a bare optional value.

`ActivePhaseReservation` is orchestration memory: never reconstruct it from a
marker, rendered prose, or conversation. Only a recorder-open phase can have a lost record.
Finished phases and phases with their own release marker cannot use repair.
Reopening a finished phase removes its release marker with the restored enrolled state.
Use `repair` to restore a lost record.
It reads berth's phase-session mapping, or the director's bare session when no other phase is open.
It confirms `active` on the board, then restores `Active` and logs and posts the repair.
Without a live mapping, repair resets an `Active` record whose reservation has board status `outstanding` or `released_after_checkpoint`.
The new state is `enrolled_awaiting_first_touch`.
The kept board answer and baselines stay unchanged.
The repair log and board post name the released reservation and its board status.
The reset writes the new state only after the log append and board post succeed.
If either fails, the record stays unchanged and repair prints no `claim` command.
Repair prints the phase's `claim` command and returns 0 when the board post succeeds.
The director then runs that command to acquire a new reservation.
Pending release, a release marker, or any other board status prevents this reset.
An unreadable board for either the recorded reservation or a mapped reservation also prevents this reset.
Never supply reservation or run ids by hand.
An existing live record stays unchanged.
`CheckpointReleaseConfirmationPending` is durable proof that the
checkpoint commit succeeded and only release confirmation remains: never create
or reconstruct it from conversation or from `git rev-parse HEAD` read at a later
time, and delete it only after <CheckpointCommit/> validates a successful
release.

An unsuccessful ending applies <RetainPhaseReservation/>. This includes
a cancelled no-diff phase, dispatch or run error, failed checkpoint commit,
every `single` run including a dirty one, user stop, drift refusal, busy ledger,
and failed release. Never invoke release from an error handler, cancellation
path, single-mode completion, run summary, or session cleanup.
</PhaseReservationContract>

<RetainPhaseReservation>
For `Active` or `CheckpointCommittedAwaitingReleaseConfirmation`, leave both the
engine reservation and `${PHASE_DIR}/delegated_phase_reservation_state.json`
intact. Report that the phase stopped before its checkpoint release could be
confirmed and name the recovery action that stopped; retain the reservation id
in durable/internal recovery output even when ordinary user-facing prose omits
tooling ids. Retention is still required when `release` may have died after
appending its checkpoint: only <CheckpointCommit/> step 7 may later confirm that
journalled success from matching outstanding evidence and delete the record.

After the coordination boundary has produced a state, a state file that is
absent, malformed, names another unfinished phase, or disagrees with a validated
claim is lifecycle loss, not non-enrollment: stop and report it. Absence before
that boundary is ordinary and must not be misreported as loss.
`RepositoryNotEnrolled` and `EnrolledAwaitingFirstTouch` require no engine
release.
</RetainPhaseReservation>

<CoordinatePhaseReservation>
Run after phase authorization and task selection, immediately before
<LaunchImplementation/>. It is the only pre-dispatch coordination boundary.
Do not dispatch until this contract reaches one of its four persisted states.

0. On resume, read this phase's state file first. A valid state for this phase
   is authoritative:

   | State | Resume action |
   | --- | --- |
   | `Active`, this phase | Resume without another claim. |
   | Either inactive state, this phase | Resume without an engine mutation; when enrolled, acquisition remains the registered edit hook's. |
   | `CheckpointCommittedAwaitingReleaseConfirmation`, this phase | The checkpoint already committed and only its release confirmation remains: resume at <CheckpointCommit/> step 7 without re-committing, re-claiming, or dispatching. |
   | Either reservation-bearing state, another unfinished phase | A stranded reservation; stop the new dispatch. |

   A completed phase removes its inactive state under <RecordPhaseCompletion/>,
   so no old inactive tag is silently reused for a new phase.
1. When no state exists yet, create `${PHASE_DIR}` and write its `files` list
   from the Work Order's Files field: one repository-relative path per line, a
   trailing `/` covering the tree below it. A phase alone needs no list; with
   two open, it is how a worker's edit finds its phase, so keep each path in
   exactly one phase's list and add a path a worker's refusal names. Then invoke
   the shared `/sync board` entry point once, under <BerthDecisions/>:

   ```sh
   set -o pipefail; cargo-berth board --json | jq -r .status
   ```

   Only the status is read, so the board's long JSON is never saved as tool
   output, and nothing needs deleting. Empty output or a non-zero exit is
   `ledger_unreadable`.

   | Result | Action |
   | --- | --- |
   | `unconfigured` | Persist `RepositoryNotEnrolled` with `phase_reservation.py enroll --board unconfigured` and proceed silently. |
   | `board_ready` | Persist `EnrolledAwaitingFirstTouch` with `phase_reservation.py enroll --board board_ready` and proceed without claiming predicted paths. |
   | `ledger_unreadable` | Stop the run; it is never opt-out. |
   | `busy` | Stop with “the ledger is busy, try again” and the exact board command to rerun; do not retry. |
2. The registered PreToolUse edit hooks own the next transition. A clear check
   atomically acquires exact `file:` scopes before the edit. A Codex worker's
   `apply_patch` runs `scripts/hooks/pre-tool-use-berth.py`, which gives each
   path to its open phase (the only one, else the one whose `files` list covers
   it, refusing a path no single list owns), checks as that phase's own berth
   session, `<this session's id>.phase-<phase>`, and replaces that phase's
   `EnrolledAwaitingFirstTouch` with `Active` from the returned facts. A path a
   sibling phase already holds is allowed and records nothing: the engine
   answers `left_unmapped` with the sibling's reservation. A Claude edit's hook
   protects the paths but writes no state, so a first touch of your own leaves
   the state for the checkpoint backstop (<CheckpointCommit/> step 5). That
   edit checks under your bare session id, and cargo-berth maps its reservation
   there, so the phase's own check later answers `left_unmapped` with it.
   The hook and the backstop record it as `Active` when the bare session maps that reservation and run.
   No other open phase may hold it.
   A sibling holds it through an `Active` state, a pending release, or its berth session map.
   With two
   phases holding reservations, your own edit is refused as ambiguous until you
   name its phase: `cargo-berth check <path> --reservation <that phase's
   reservation id> --json` acquires the path for it, and the edit then clears. A blocked check refuses the edit and
   leaves the pending state intact; a worker reports the refusal to you, and you
   answer the overlap under <BerthDecisions/>. An unreadable ledger stops
   without facts. Beyond those answers, do not call `cargo-berth claim` on
   behalf of a Work Order.
</CoordinatePhaseReservation>
