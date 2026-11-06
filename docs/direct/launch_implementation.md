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
5. Apply <LongLivedWorkers/>. For Codex, run
   `~/.claude/scripts/lib/py ~/.claude/scripts/agents/codex_mesh.py list --session-dir
   "${SESSION_DIR}"`. Launch both in one message, `impl` first, each under
   <ToolingContract/>. When the slot has an open worker, pass its full roster name
   with `--to`; the phase prompt is its follow-up message. Omit `--to` only for
   a slot with no reusable thread; the first use and any replacement after an
   end, overflow failure, or retirement therefore receive an opening prompt
   without `--to`. Later phases normally run:

   ```sh
   bash ~/.claude/scripts/direct/assign.sh --role implement --to "<full impl worker name>" \
     --phase "<phase id>" --session-dir "${SESSION_DIR}" --cwd "${WORKING_DIR}" \
     --prompt "${PHASE_DIR}/implementation_prompt.md" --paths "${PHASE_DIR}/implementation_paths.txt" &
   bash ~/.claude/scripts/direct/assign.sh --role test --to "<full test worker name>" \
     --phase "<phase id>" --session-dir "${SESSION_DIR}" --cwd "${WORKING_DIR}" \
     --prompt "${PHASE_DIR}/test_prompt.md" --paths "${PHASE_DIR}/test_paths.txt" &
   ```

   Every launch, a slot's first included, carries `--phase "<phase id>"`: the
   pass is stamped with that phase and the launcher writes its status, summary
   and log to `${PHASE_DIR}`, so a phase running beside another never reads
   that one's files.

   Change `--role` to assign another role to the same worker.
   Responsibility follows <ProgressContract/>. **Both workers carry a pass
   kind**, so a team phase records two passes and stops being attributed to one
   agent. The kind is the work the worker was assigned and nothing more — it names,
   it never triggers, so a worker never misreports its work to avoid a side effect.
   **Task and kind are the same word** on every worker: the fourth argument selects
   the agent and the sixth records the pass, and both say what this worker is
   doing. The vocabulary is `impl`, `test`, `fix`, `review` — nothing else
   resolves, in `models.conf` or in the ledger.
6. Announce prompt, board, and heartbeat paths, set `EARLY_REVIEW=none`, then
   apply <DispatchContract/> once for the whole team.
7. Read `${PHASE_DIR}/impl_status_impl` and `impl_status_test` after completion. Both statuses must be terminal.
   Load `impl_summary_impl.txt` into `${IMPL_SUMMARY}` when `impl` reports `implemented`.
   Read `impl_summary_test.txt` for the test results and findings.
   Apply <WorkerLaunchFailure/> first when `impl` reports an error.
   `assign.py` ends a run when its only worker command fails and no pass completed.
   Each recorder slot counts as one worker. A Claude launch must add a `pass_started` event for its slot.
   The director must hold only that run. A launcher signal does not trigger this path.
   For herdr, only exit 2 qualifies. Exits 3 through 8 retain the run.
   The launcher calls `progress_history.py finish-run --session-dir <run> --status error`, then `end_session.sh`.
   It keeps the launch exit code and the phase reservation. A failed cleanup child reports its error.
   For other genuine errors, cancel the early reviewer per <EarlyReviewArm/> and apply <RetainPhaseReservation/>.
   Report `impl_agent_impl.log`, record `finish-run --status error`, run `end_session.sh`, and stop.
   Emit <RunSummary/> for a run with multiple phases.
8. `implemented` is the worker's claim, not a passed gate. Read
   `${IMPL_SUMMARY}` for a verification line it left running, unread, or
   unmentioned — "still running", "I'll report once it completes", a listed
   command with no stated result. When one is there, run that command yourself
   under <VerificationContract/> before <DualReview/> and review against its real
   output. A failing gate the worker never read is a finding like any other,
   not a reason to reject the phase.
</LaunchImplementation>
