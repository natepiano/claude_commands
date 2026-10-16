# Launch implementation

Read at the point of use from `/unit:direct`. Defines `<LaunchImplementation/>`
in full.

**Read when:** before a phase's first dispatch, and again when that dispatch
completes.

<LaunchImplementation>
1. Once per phase, create `${PHASE_DIR}` if it is missing (ad hoc work uses
   `${SESSION_DIR}/phases/adhoc`), then save `git rev-parse HEAD` to
   `${PHASE_DIR}/phase_baseline` and `git status --short` to
   `${PHASE_DIR}/progress_baseline_status` before dispatch. Stop if either
   fails; fixes retain both baselines.
2. Close the outgoing phase before opening this one, per <PassOwnership/>: when a
   finished phase's record is still active, run `progress_history.py finish-phase
   --session-dir "${SESSION_DIR}" --status completed` first. A phase opened
   beside one still running leaves that one open. Then run
   `progress_history.py start-phase --session-dir "${SESSION_DIR}"
   --phase-id <id> --phase-title <title> --work-order-file
   "${PHASE_DIR}/implementation_prompt.md"`. Use `ad hoc` plus scope without a
   phased plan; pass the original prompt only. Both run before the dispatch in
   step 4, never after it.
3. `~/.claude/config/agents.conf` owns delegate family/model/effort, one row
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
   `python3 ~/.claude/scripts/agents/codex_mesh.py list --session-dir
   "${SESSION_DIR}"`. Launch both in one message, `impl` first, each under
   <ToolingContract/>. When the slot has an open worker, pass its full roster name
   with `--to`; the phase prompt is its follow-up message. Omit `--to` only for
   a slot with no reusable thread; the first use and any replacement after an
   end, overflow failure, or retirement therefore receive an opening prompt
   without `--to`. Later phases normally run:

   ```sh
   implement.sh --to "<full impl worker name>" --phase "<phase id>" \
     "${SESSION_DIR}" "${WORKING_DIR}" \
     "${PHASE_DIR}/implementation_prompt.md" impl \
     "<responsibility>" impl "<activity>" 0 impl
   implement.sh --to "<full test worker name>" --phase "<phase id>" \
     "${SESSION_DIR}" "${WORKING_DIR}" \
     "${PHASE_DIR}/test_prompt.md" test \
     "<responsibility>" test "<activity>" 0 test
   ```

   Every launch, a slot's first included, carries `--phase "<phase id>"`: the
   pass is stamped with that phase and the launcher writes its status, summary
   and log to `${PHASE_DIR}`, so a phase running beside another never reads
   that one's files.

   A worker assigned another role swaps both role words and nothing else — the
   open `test` worker continuing as a writer is
   `implement.sh --to "<full test worker name>" --phase "<phase id>" "${SESSION_DIR}" "${WORKING_DIR}" "${PHASE_DIR}/test_prompt.md" impl "<responsibility>" impl "<activity>" 0 test`.
   Responsibility follows <ProgressContract/>. **Both workers carry a pass
   kind**, so a team phase records two passes and stops being attributed to one
   agent. The kind is the work the worker was assigned and nothing more — it names,
   it never triggers, so a worker never misreports its work to avoid a side effect.
   **Task and kind are the same word** on every worker: the fourth argument selects
   the agent and the sixth records the pass, and both say what this worker is
   doing. The vocabulary is `impl`, `test`, `fix`, `review` — nothing else
   resolves, in `agents.conf` or in the ledger.
6. Announce prompt, board, and heartbeat paths, set `EARLY_REVIEW=none`, then
   apply <DispatchContract/> once for the whole team.
7. On completion, read `${PHASE_DIR}/impl_status_impl` and `impl_status_test`; the phase is
   done only when both are terminal. `implemented` on `impl` loads
   `impl_summary_impl.txt` into `${IMPL_SUMMARY}`; read `impl_summary_test.txt`
   for what it completed and for findings it posted. If `impl` errors, apply <DelegateLaunchFailure/>
   first — a worker that died in seconds is resolved there, not reported. A
   genuine error then cancels any early-launched
   reviewer per <EarlyReviewArm/>, applies <RetainDelegatedPhaseReservation/>,
   reports `impl_agent_impl.log`, records
   `finish-run --status error`, runs `end_session.sh`, and stops; multi-phase
   runs also emit <RunSummary/>.
8. `implemented` is the delegate's claim, not a passed gate. Read
   `${IMPL_SUMMARY}` for a verification line it left running, unread, or
   unmentioned — "still running", "I'll report once it completes", a listed
   command with no stated result. When one is there, run that command yourself
   under <VerificationContract/> before <DualReview/> and review against its real
   output. A failing gate the delegate never read is a finding like any other,
   not a reason to reject the phase.
</LaunchImplementation>
