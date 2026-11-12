# Held Astra roles

<HeldAstraRoles>
The director gives the brief and accepts the design. Astra writes the assigned design.
Use one `design_roles.py` designer and at most one reviewer per design.
`models.conf` selects the role's model and effort.
Held roles use `worker.py` with explicit `astra-designer` or `astra-reviewer` roles.
Keep the worker's row, native conversation, and pane across rounds, phases, and runs.
An ad hoc Astra review outside the staff hierarchy uses a consultation pane through `assign.sh`.
The consultation creates no staff row.
`side_pane.py send|close` uses the same terminal for later questions and closure.
`design_roles.py` saves each headed prompt and reply. Do not also use `astra_work.py brief` for that role.
For another designer, include `config/design_roles/designer.md` in its brief.
Save the brief with `astra_work.py brief --run-directory <SESSION_DIR> --phase <id>
--member <worker name> --document <path> --round <N> --text-file <brief>`.
Save its reply as `<SESSION_DIR>/design/phase<id>/round<N>_reply.md` without overwrite.
Pass both `--run-directory` and `--phase` to `astra_review.py` calls for a phase.

For DelegatedPhaseReservationContract, prepare exact phase files, baseline and
reservation before dispatch. Bind the designer to those output paths with explicit
`PhaseBoundDesigner` access; reviews use `ReadOnlyReviewer`. Repository edits use
patches. Finish a turn and rebind while idle before adding a path or changing scope.
The holder is the worker's staff parent.
A stale binding, recovery state, adoption fence or unavailable holder blocks dispatch.

For DualReview and FixDispatch, keep the same reviewer and writer. Save findings,
responses and short rulings that cite files; update the Work Order for a scope
change. Busy or unavailable review is not a pass and permits no replacement.

For PhaseCleanup, FinalGate and RunSummary, finish turns, release the reservation, then unbind the design.
Keep conversations, pending messages and evidence until explicit end.
Account moves and holder restarts resume the same conversations through the role lifecycle.
Never replace a lost conversation silently. Adoption uses its recorded transfer and destination.
Keep the existing checkpoint, visual check and gate order.
For rollback, set `[roles] new_open=false` in `config/design_roles.conf`.
Finish active turns through the holder lifecycle.
Keep placements, fences, pending input, prompts, replies and unfinished reservations.
Keep adopted ownership at its destination unless a reverse transfer proves no destination writer.
Restore old command text only when it preserves these fences.
</HeldAstraRoles>
