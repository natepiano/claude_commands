---
description: Commit and push a finished /unit:direct phase and release its reservation, when the run did not.
---

# Direct — checkpoint

**Usage:** `/unit:checkpoint`

Type this when a phase finished and was never committed, or when a run stopped
between the commit and the reservation release. It runs inside the current
session and already knows the phase, the plan doc, and the mode. If no `/unit:direct`
run is active, say so in one line and stop.

This is durable state with no cheap undo. Read the whole contract before acting
on any part of it, and read the reservation record from disk — a value
remembered from conversation, from the harness session mapping, or re-derived
from current `HEAD` is not proof and will silently accept the wrong checkpoint.

`/unit:direct` reads this file once per completed phase in loop and verbose
mode. It defines `<CheckpointCommit/>`, `<ShrinkCommit/>`, and
`<PushCheckpoint/>` in full. `single` never commits.
Phase-end order: `~/.claude/docs/direct/phase_end.md` → <PhaseEnd/>.

Everything below is the contract.

---

<CheckpointCommit>
Loop/verbose only:

1. Require smoke pass, `not applicable`, or `deferred`, and a `converged`
   phase findings ledger per <FindingsLedger/>. An off check reports `Off`.
2. Confirm status contains only this phase, source-comment edits left by the
   previous phase's plan review, its plan doc, and an approved change to
   `${NEXT_ITEMS_PATH}` when present. The current phase may still have its Work
   Order. For a header `**Standing:** style_plan.py`, expect no plan doc in
   status: it is excluded, and earlier done ticket phases keep their Work
   Orders until refill discards them. Otherwise refuse the checkpoint if any
   earlier `done` phase still has a Work Order; its shrink must be repaired first.
3. Formatting is covered by <PhaseChecks/>; do not rerun it here.
4. Mark the phase `status: done`. For a standing plan, defer this until step 6's
   ticket checkpoint succeeds. Never put its commit hash in the plan.
5. First record a first touch no hook recorded. When the state is still
   `EnrolledAwaitingFirstTouch`, this checks every path of this phase changed
   since `${PHASE_DIR}/phase_baseline` and writes `Active` from the clear
   answer. When the state is `Active`, it reads the recorded reservation on
   berth's board (`cargo-berth board --reservation <id> --json`). A released one
   takes the phase's live reservation from the same check, so drift never names
   a released id; with no live one to take, it exits 1 and names the stale id.
   Every other state it leaves as it is:

   ```sh
   phase_reservation.py record-active --session-dir "${SESSION_DIR}" --phase <phase id>
   ```

   Exit `1` means the check was refused: answer the overlap it names under
   `/unit:direct` <BerthDecisions/>, then run it again. An exit `1` that names a
   released reservation with no live one blocks the checkpoint: apply
   <RetainPhaseReservation/> and report the line. Then read and validate
   `${PHASE_DIR}/delegated_phase_reservation_state.json`; never use a value
   remembered from conversation or the harness-session mapping.

   | Durable state | Action |
   | --- | --- |
   | `Active`, phase current | Run the full phase-start drift comparison below exactly once. |
   | `RepositoryNotEnrolled`, `EnrolledAwaitingFirstTouch` | Skip drift; neither owns a reservation. |
   | `CheckpointCommittedAwaitingReleaseConfirmation` | Valid only as a resumed state: route directly to step 7, re-entering neither drift nor checkpoint creation. |

   Run each cargo-berth command in this checkpoint as written, under
   `/unit:direct` <BerthDecisions/>:

   ```sh
   cargo-berth drift --full --reservation <recorded-reservation-id> --json
   ```

   Name the reservation: each open phase holds its own, so a drift that picks
   one for itself is ambiguous whenever a phase runs beside this one.

   Require an exit-`0` result with `payload.kind = drift`,
   `payload.data.comparison = full_phase_start`, and exactly one
   `payload.data.results` entry whose `reservation_id` equals the
   `reservation_id` in the durable `ActivePhaseReservation`. Bind that result to
   the record's `phase_start_head`, because `full_phase_start` means the engine
   compared the selected reservation against its protected phase-start baseline.
   The response does not echo that object id, so do not invent an OID field or
   re-derive acting identity. The cheap drift delta is forbidden here.

   Anything else is identity ambiguity or unsafe drift and blocks the
   checkpoint: apply <RetainPhaseReservation/> and report the exact
   full-drift command above so a person can run it to see the conflict. That
   covers no result, more than one, a different reservation id, any comparison
   other than `full_phase_start`, an incursion, collision, attribution
   requirement, absent or corrupt ledger, a malformed response, and the
   `Unidentified` identity case — an exit-`5` `invalid_input` whose diagnostic
   says drift requires a live session mapping, active coordination-run marker,
   or `CARGO_BERTH_RUN`. Exit `6` has already spent the engine's single
   ten-second deadline: invoke no retry.
6. **Standing plans:** rerun the ticket's
   `~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py gate --repo . --ticket <id>`
   before staging. Exit 1 is a review: it retains the reservation and returns
   to repair. Exit 2 means the gate could not run (`error: <reason>`, such as a
   locked ledger): retain the reservation, do not repair, and rerun the gate.
   If the same error comes back, stop and report it.
   On exit 0, stage only this phase's code paths from
   `git status --porcelain`, handling renamed paths as pairs; never name the
   plan doc, `docs/style-production/` or pinned rule files in `git add`.
   Commit exactly the gated tree once using the message below, then capture
   and persist its full id through the durable transition below, adding
   `--ticket <ticket>`: the record then holds the reservation for that
   ticket's merge, and this checkpoint releases nothing. Before marking
   the phase done, run
   `~/.claude/scripts/lib/py ~/.claude/scripts/style/style_ledger.py ticket checkpoint --repo . --id <ticket> --commit <captured code commit> --session-dir "${SESSION_DIR}" --phase <phase id>`.
   The ledger keeps the run folder and phase, and `ticket merge` releases the
   held reservation from them. berth's release counts when its protected tip
   is the checkpoint, or a later commit of the unit's branch, such as a merge
   of style/main, that the style branch tip contains.
   If recording fails, retain the commit and reservation, repair the ticket
   record, and do not create another commit.

   **No-change standing ticket:** with every cell recorded `clean`, `exception`
   or `held` at the unit's HEAD and no tracked diff, make no commit. Capture
   `git rev-parse HEAD` once and require
   `git merge-base --is-ancestor <captured HEAD> refs/heads/<style merge branch>`.
   The standing-unit fast-forward exception in `docs/production_format.md`
   item 5 keeps HEAD on the style merge branch's history after a phase-start sync.
   On failure, report one line to the style showrunner and leave the ticket
   assigned until it answers. On success, from `Active`, replace the durable
   record with `CheckpointCommittedAwaitingReleaseConfirmation` carrying that
   same captured full id as `checkpoint_commit`, with `phase_reservation.py
   committed --session-dir "${SESSION_DIR}" --phase <phase id> --commit <captured HEAD> --ticket <ticket>`,
   and read it back. `--commit` takes any name of that commit, a short hash
   too, and the record keeps its full id. This is the one case where
   that state carries a commit the phase did not make. A failed transition or
   read-back retains the valid durable state and runs no ticket merge. Then run
   `style_ledger.py ticket checkpoint --repo . --id <ticket> --commit <captured HEAD> --session-dir "${SESSION_DIR}" --phase <phase id>`
   then `style_ledger.py ticket merge --repo . --id <ticket>` through
   `~/.claude/scripts/lib/py ~/.claude/scripts/style/style_ledger.py`. The
   merge releases the held reservation and clears the record. An exit `2`
   that ends `rerun ticket merge` keeps the reservation held: report its line
   and rerun the merge after the cause is fixed.
   For each `has no applied check` line that merge prints, check the change in
   the code and run the printed `held built` command when the change is there.
   A finding whose change is not there waits for a person, who makes the change
   and runs that command, or waives the finding with `style_ledger.py held waive --person <name>`.
   Run step 7; a record the merge cleared, and an inactive state, release
   nothing. After confirmation, mark the
   phase done, skip staging, <PushCheckpoint/> and the merge request, send
   `From <unit>: ticket <id> closed with no code change; <n> held.`, and refill
   at once through <StandingPlanRefill/>.

   **Other plans:** stage the phase, its plan doc, and `${NEXT_ITEMS_PATH}` when
   approved and changed. Every code checkpoint uses this message:

   ```
   checkpoint(<plan-slug>): phase N — <title>

   <what the phase built>

   Change-Review: <what changed for you> | <how to try it: where and what to type> | <what you see>
   Claude-Session: <session url>
   ```

   **Cargo repositories:** every `checkpoint(...)` commit needs this check:
   phase checkpoints, the final gate, one-off fix commits, and merges concluded
   with a checkpoint subject. After staging, run
   `~/.claude/scripts/direct/verify.sh doc` as its own command and as the last
   command before `git commit`. `doc` runs rustdoc on a checkout of the
   staged tree in a stable, locked checkout under the repo's target directory. Unstaged and untracked files are not in that
   tree, and a plain `git commit` leaves them out too. With `-a`, `--only`,
   `--include` or paths, stage those changes before `doc`: the record must be
   for exactly the tree the commit writes. On failure, repair through
   <FixDispatch/>, stage the repairs, then run `doc` again. The checkpoint
   hook refuses a commit without a passing record for its exact tree;
   `lint.conf` cannot disable this gate. `doc` uses `CARGO_TARGET_DIR` when set,
   else the repo-top metadata target. Its private index updates the stable
   checkout with `read-tree -m -u`, preserving unchanged mtimes and dependency
   artifacts; a damaged checkout gets one fresh checkout. Workers may run `doc` at any
   time; their Work Orders do not require it.

   **Every repository:** run the checkpoint commit as the first command in
   its own Bash call. Put `cd`, `git add`, and all other commands in earlier
   calls. Commands after the commit, such as a push, are allowed.

   The gate catches ordinary agent commit forms, not deliberate hiding in
   scripts, aliases or functions from earlier calls. It is not a shell interpreter.
   A checkpoint is a line of the commit's own message matching `checkpoint(...):`.
   Only a `git` command word (or `/path/git`), after assignments and
   `env`/`command`/`exec`/`sudo`/`nice`/`time`, starts a commit or merge.
   `env -C`/`--chdir` and `git -C` select its repo. Conditions, ordinary for
   loops, while/until loops, case arms, groups, `!`, `time` and function bodies
   inspect command words; quoted arguments and heredoc bodies never count.
   The hook reads literal `-m`, `-F`, reused subjects (`--amend`, `-C`/`-c`),
   and commit `MERGE_MSG`/`SQUASH_MSG`. Unreadable messages, including shell
   expansions and the `"$(cat <<'EOF' … EOF)"` form, count only in Cargo repos.
   A merge without a message option uses git's ordinary default subject;
   `--log` does not supply a message. Literal merge messages follow every
   checkpoint rule; `-e`/`--edit` and unreadable `-F` count only in Cargo repos.
   Opaque commands (`eval`, expanded command words, sh/bash/zsh `-c`, unmodeled
   `env` options, `coproc`, `select`, `[[ ]]`, `(( ))`, arithmetic for) refuse
   when their own quote-removed words hold `git` with `commit`/`merge` and
   `checkpoint(`, in every cwd. Without checkpoint text they refuse only in
   a Cargo cwd with git commit/merge words. Unrelated `$EDITOR file` and
   `eval "$(direnv hook zsh)"` pass. Options end at `--`; later `--fixup` or
   `--squash` words are paths and require the exact tree record.

   For these commits, the hook refuses `--git-dir`, `--work-tree`, `GIT_DIR=`,
   `GIT_INDEX_FILE=`, and `GIT_WORK_TREE=` overrides, and `git -C` directories
   it cannot resolve, including shell expansions and unquoted globs. In a
   Cargo repository it also refuses `-p`/`--patch`/`--interactive`,
   `--pathspec-from-file`, paths with expansions or globs, conflicting
   selection options, and a checkpoint merge. For a checkpoint merge, run
   `git merge --no-commit` with a literal `-m` message that is not a
   checkpoint, stage the result, run `doc`, then commit with the checkpoint
   subject. Each refusal names the fix: remove overrides, or use a literal
   directory, path or command.

   Every `checkpoint(...)` commit that changes more than Markdown carries
   `Change-Review: <what changed for you> | <how to try it: where and what to type> | <what you see>`, one line per visible change, or
   `Change-Review: none — <why nothing visible changes>`. Write each line for a
   person with no context. "Where" names the machine (natedev or the Mac) and
   the session (a terminal, a showrunner session, a unit session, any Claude
   session). Example:
   `Change-Review: The unit board shows each unit's phase | In a showrunner session on natedev, type /showrunner:unit_board | Each row has a Phase column`.
   A line without all three parts is refused.
   A change the user can check outside the app counts as visible too: a
   table's columns, a status line, a command's output.
   A hook refuses the commit without it and the merge refuses the checkpoint;
   the merge turns each visible line into a row on the change-review list.

   After every successful checkpoint commit, enrolled or not, run `python3 ~/.claude/scripts/direct/progress_history.py checkpoint --session-dir "${SESSION_DIR}" --phase <phase id> --commit <full hash>`.
   A failed commit is an unsuccessful ending: do not invoke release, leave
   `Active` untouched, and apply <RetainPhaseReservation/>. After a
   successful commit from `Active`, immediately capture the full output of
   `git rev-parse HEAD` and replace the `Active` record with
   `CheckpointCommittedAwaitingReleaseConfirmation` carrying that value as
   `checkpoint_commit`:

   ```sh
   phase_reservation.py committed --session-dir "${SESSION_DIR}" --phase <phase id> --commit <captured commit>
   ```

   `--commit` takes any name of the commit, and the record keeps its full id;
   a name that is no commit is refused with exit `2`. A standing ticket adds
   `--ticket <ticket>`, as above. Then read the new record back. Never write the record by hand. That durable transition
   must succeed before release is invoked, and its value comes only from the
   commit that just succeeded (or the ancestry-checked, captured HEAD of the
   no-change standing ticket above), never from conversation or from a later
   reading of `HEAD`. If capture, atomic replacement, or read-back fails, do not invoke
   release and apply <RetainPhaseReservation/> to whichever valid
   durable state remains.
7. Read and validate the durable record again.

   For a standing ticket with a pending release record, finish any interrupted
   `ticket checkpoint` using that record's `checkpoint_commit` before marking
   the phase done; repeating the same checkpoint is safe.
   A no-change ticket is already `merged` and needs no repeated transition.

   | State | Action |
   | --- | --- |
   | `CheckpointCommittedAwaitingReleaseConfirmation` naming `held_for_ticket` | A standing ticket's reservation stays held: invoke no release and keep the record. `style_ledger.py ticket merge` releases it. Mark the phase done and continue at step 8. |
   | `CheckpointCommittedAwaitingReleaseConfirmation` | Read its reservation id and `checkpoint_commit` from the read-back `CheckpointReleaseConfirmationPending`, then invoke `/sync release` exactly once. |
   | No record, and `reservation_released` names the checkpoint | A no-change ticket's merge released it: invoke no release. |
   | `Active` | Step 6's durable transition did not complete: do not invoke release; apply <RetainPhaseReservation/>. |
   | Either inactive state | Invoke no release. |

   ```sh
   cargo-berth release <recorded-reservation-id> --json
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
   cargo-berth board --reservation <recorded-reservation-id> --json
   ```

   Inspect `payload.data` in its output, without reading `message`. Exit `0`
   requires `kind = reservation_lifecycle`, the requested `reservation_id`, and exactly
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
   first-attempt structured assertions or these recovery assertions pass, clear
   the record; never delete it by hand:

   ```sh
   phase_reservation.py clear --session-dir "${SESSION_DIR}" --phase <phase id> --released <read-back checkpoint_commit>
   ```

   `--released` also takes any other name that git resolves in `--repo`
   (default `.`) to that commit; a name git refuses, an ambiguous short hash
   among them, keeps the record with exit `2`.

   An ordinary failed release did not append anything because transaction
   validation precedes the journal write; it is not recovery, and the original
   first-attempt assertions still apply when it is run again. At the moment of
   any busy or failed release, invoke no retry and apply
   <RetainPhaseReservation/>. The checkpoint commit exists, but the
   phase does not complete until a later invocation confirms either the normal
   first-attempt reply or the matching already-journalled checkpoint above.
8. Run <PushCheckpoint/>. Outside a production, report
   `Checkpoint <short hash> — phase N: <title>.` with its push note, if any.
   This report follows successful release when the phase was active.
9. In a production unit, run
   `python3 ~/.claude/scripts/direct/progress_history.py review-trial --session-dir "${SESSION_DIR}"`
   and `python3 ~/.claude/scripts/direct/phase_table.py parallel --session-dir "${SESSION_DIR}"`,
   and keep both lines for <ProductionUnit/> item 3's checkpoint notice.
</CheckpointCommit>

<ShrinkCommit>
Standing plans (`**Standing:** style_plan.py`) skip this entire section: no
shrink, shrink commit, shrink notice or `--shrink` merge request; never stage
their excluded plan or pinned rule files.

Loop/verbose only, after <RunPhaseShrink/> passes its structural check. The
next phase's workers may be editing, but they never stage files.

1. Stage only `${PLAN_DOC}` and an approved, changed `${NEXT_ITEMS_PATH}`.
   Inspect the staged path set. Refuse the commit if it contains any other
   path; leave the workers running and repair the staged set before retrying.
2. Commit only the staged paths, once:

   ```
   shrink(<plan-slug>): phase N — <title>

   Claude-Session: <session url>
   ```

3. Run <PushCheckpoint/>. In a production, give the hash and push note, if any,
   to the caller for <ProductionUnit/> item 3's shrink notice. Outside a
   production, report the shrink hash and push note, if any.

This commit owns no reservation. Do not release a reservation here. In an
enrolled repository, the shrink's first plan-doc write uses the Edit tool, so
the edit hook claims the plan doc for the running phase's reservation
(<PhaseEnd/> step 7). A failed structural check blocks this commit
only. Repair the shrink before the next checkpoint; that checkpoint refuses
while an earlier `done` phase still has a Work Order.
</ShrinkCommit>

<PushCheckpoint>
Puts each commit on origin, so the work survives this machine. Runs after a
successful release in <CheckpointCommit/>, and after <ShrinkCommit/>,
<FinalGateCommit/>, and <AsBuiltCommit/>. Run each git command with
`dangerouslyDisableSandbox: true`.

1. **Branch.** The current branch, under its own name. On the default branch
   (`gh repo view --json defaultBranchRef -q .defaultBranchRef.name`), push to
   `direct/<plan-slug>` instead, so unfinished work never lands there.
2. **Ancestry.** `git fetch origin <branch>`; a branch origin lacks yet is
   fine. If origin has it and
   `git merge-base --is-ancestor origin/<branch> HEAD` fails, do not push. The
   note is `push skipped: origin/<branch> has commits this branch lacks`.
3. **Push.** `git push -u origin <branch>`; on the default branch,
   `git push origin HEAD:refs/heads/direct/<plan-slug>`. Never force. Run
   each step as its own command, never joined to another with `;` or `&&`:
   the allow list covers `git push` alone, and a joined line goes to the
   auto-mode classifier as a whole.
4. **Failure.** A failed push does not fail the commit, and never stops the
   run. An auto-mode refusal is a failed push. The note is
   `push failed: <reason>`, and the next push carries both. For
   `sign_and_send_pubkey: signing failed … agent refused operation` or
   `Permission denied (publickey)`, run `github-warm-status`; when it reports
   cold, the note asks the user to run `github-warmup` at a terminal on this
   machine. Never `gh auth login`, a new key, or an HTTPS remote.
</PushCheckpoint>
