# Launch implementation

Read at the point of use from `/unit:delegate`. Defines `<LaunchImplementation/>`
in full.

**Read when:** before a phase's first dispatch, and again when that dispatch
completes.

<LaunchImplementation>
1. Once per phase, save `git status --short` to
   `${SESSION_DIR}/progress_baseline_status`; fixes retain it.
2. Close the outgoing phase before opening this one, per <PassOwnership/>: when a
   phase record is still active, run `progress_history.py finish-phase
   --session-dir "${SESSION_DIR}" --status completed` first. Then run
   `progress_history.py start-phase --session-dir "${SESSION_DIR}"
   --phase-id <id> --phase-title <title> --work-order-file
   "${SESSION_DIR}/implementation_prompt.md"`. Use `ad hoc` plus scope without a
   phased plan; pass the original prompt only. Both run before the dispatch in
   step 4, never after it.
3. `~/.claude/config/agents.conf` owns delegate family/model/effort, one row
   per kind. Each seat's kind is its opening role from the Work Order's
   `Seats:` field: `impl` for the `impl` slot, and for `test` whatever its
   Seats line opens it as — `test` under the default opening. State the
   opening in the dispatch update in ordinary words: "opening 2 writers: impl
   on the hana side, test writing the catalyst side".
4. Take the partition and the opening from Seats and write one prompt per slot
   under <WritePromptContract/>: `${SESSION_DIR}/implementation_prompt.md` for
   `impl` and `test_prompt.md` for `test`. Only when the field is absent, partition per <TeamFilePartition/>
   yourself and say so in the dispatch update.
5. Launch both in one message, `impl` first, each under <ToolingContract/>.
   The fourth and sixth arguments are the seat's opening role, the same word in
   both places; the default opening is:

   ```sh
   implement.sh "${SESSION_DIR}" "${WORKING_DIR}" \
     "${SESSION_DIR}/implementation_prompt.md" impl \
     "<responsibility>" impl "<activity>" 0 impl
   implement.sh "${SESSION_DIR}" "${WORKING_DIR}" \
     "${SESSION_DIR}/test_prompt.md" test \
     "<responsibility>" test "<activity>" 0 test
   ```

   A seat Seats opens in another role swaps both words and nothing else — the
   `test` seat opening as a writer is
   `implement.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/test_prompt.md" impl "<responsibility>" impl "<activity>" 0 test`.
   Responsibility follows <ProgressContract/>. **Both seats carry a pass
   kind**, so a team phase records two passes and stops being attributed to one
   agent. The kind is the work the seat was assigned and nothing more — it names,
   it never triggers, so a seat never misreports its work to avoid a side effect.
   **Task and kind are the same word** on every seat: the fourth argument selects
   the agent and the sixth records the pass, and both say what this seat is
   doing. The vocabulary is `impl`, `test`, `fix`, `review` — nothing else
   resolves, in `agents.conf` or in the ledger.
6. Announce prompt, board, and heartbeat paths, set `EARLY_REVIEW=none`, then
   apply <DispatchContract/> once for the whole team.
7. On completion, read `impl_status_impl` and `impl_status_test`; the phase is
   done only when both are terminal. `implemented` on `impl` loads
   `impl_summary_impl.txt` into `${IMPL_SUMMARY}`; read `impl_summary_test.txt`
   for what it completed and for findings it posted. If `impl` errors, apply <DelegateLaunchFailure/>
   first — a seat that died in seconds is resolved there, not reported. A
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
