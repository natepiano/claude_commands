# Delegate — status report

**Usage:** `/plan:delegate_report`

Type this when a run has gone quiet, when an update arrived without its tables,
or any time you want to know what the agents are doing right now. It runs inside
the current session and already knows the session directory, the phase, and
which dispatches are live. If no delegate run is active, say so in one line and
stop.

`/plan:delegate` reads this file at every timer tick and poll timeout. It defines
`<ProgressReport/>` — the content of an update. `<ProgressContract/>` in
`~/.claude/commands/plan/delegate.md` keeps the timing rules that say when one is
owed. Never compose a report from memory of an earlier read: the byte-for-byte
copy rule and the plain-English closing sentences are the parts that decay.

Everything below is the contract.

---

<ProgressReport>
1. Check launcher state first. For Codex, `exit_code` alone marks terminal
   completion; a returned `session_id` without it remains active. If no dispatch
   remains active, emit no stale report and process completion. On Claude, also
   stop and clear any timer when its dispatch completes first.
2. Read the current Work Order and verification list, the latest relevant
   heartbeat lines, `board.sh read "${SESSION_DIR}" --since <cursor>`,
   `git status --short`, and `git diff --stat` in `${WORKING_DIR}`. Keep the
   board cursor across ticks, compare status with the phase baseline, include
   untracked paths without changing the index, and read the board's `handoff`
   posts for which slot holds which role right now.
3. Derive the **current-phase** percentage from completed and remaining work,
   changed areas, current activity, and passed verification—not elapsed time.
   Stay below 20 until implementation appears; editing is the middle; completed
   verification lines are the final stretch; reviews advance by inspected scope.
   Cap at the last factually passed stage:
   `implementation` 75, `initial_review` 85, `open_findings` 90, `closure` 95,
   `checkpoint` 98, or `complete` 100.

   **Do not derive the whole-plan percentage.** The recorder computes it from
   the plan's phase headings and overwrites whatever `--project-raw-percent`
   and `--project-percent` carry, so pass the phase percentage there and treat
   the project value as advisory. Never count phases by hand or by grep: a
   heading takes three forms over its life—`· status: todo`, `· status: done`,
   and the shrunk as-built form that drops the status marker for a commit
   annotation—and any pattern keyed on `status:` silently ignores every
   archived phase. That mistake reported a 68%-complete plan as 36%. To read the
   count directly, without an active phase and pass:

   `python3 ~/.claude/scripts/delegate/progress_history.py phase-count --plan-doc "<plan>" [--phase-percent N]`
4. Apply <EarlyReviewArm/>: steps 2-3 are its evidence and this tick is its only
   trigger point. It runs **before** the recorder, so a reviewer it launches is
   already working in the round table this tick prints.
5. Run:

   `python3 ~/.claude/scripts/delegate/progress_history.py calibrate --session-dir "${SESSION_DIR}" --candidate-percent "${PHASE_RAW_PERCENT}"`

   Use its phase suggestion when applicable; otherwise keep the raw value. Then
   run:

   `python3 ~/.claude/scripts/delegate/progress_history.py progress --session-dir "${SESSION_DIR}" --project-raw-percent "${PROJECT_RAW_PERCENT}" --project-percent "${PROJECT_RAW_PERCENT}" --phase-raw-percent "${PHASE_RAW_PERCENT}" --phase-percent "${PHASE_REPORTED_PERCENT}" --cap-stage "<stage>" --activity "<current activity>" [--phase-override-reason "<specific evidence>"]`

   **`No open window to report` does not mean the dispatch finished.** A pass
   can be recorded closed while its worker is still running — a live run has
   shown two seats carrying a closed pass while both were posting to the board —
   so treating it as completion would route a healthy round through
   <FixDispatch/>'s abandon path and lose the work. Establish which it is from
   the seats themselves: `impl_status_<slot>`, board posts since the last
   cursor, and whether the launcher has exited. Alive means say so in prose and
   keep the run going; genuinely terminal means step 1's completion handling.
   Never open an activity to make the tables render — that records main-agent
   work that never happened.

   Include the override reason only when rejecting an applicable calibrated
   value. **Copy its Markdown output byte-for-byte** — the scope line,
   both tables with every row and cell, the `Earlier:` line above the round
   table when there is one, the delegates lines, and the wall clock.
   Never reorder, edit, or omit any part of it: the recorder owns every column,
   duration format, and ETA band. When step 4 has armed an early reviewer, say
   below the tables that the writer and the reviewer are working at once.
6. Read the round table as seats. It leads with `Stage`, `Start`, and `Elapsed`
   — `Stage` rather than `Round` because a row is not always a round: a
   verification, a smoke run, or a lone reviewer each own one. Its three seat
   columns are `Agent 1`, `Agent 2`, `Agent 3` — the slots `impl`, `test`,
   `review` in that order, identities that never change, numbered so the header
   carries no role word the cell beneath could contradict. Each cell is the role
   that seat held over the row's stretch, how long it held it, and what the seat
   is doing at the end of it:

   - `running` — its window is open and its last board line is work.
   - `waiting` — open, but the seat says it is held up: on a peer's edit, on the
     cargo token, on a gate. Three seats reading `running` while two sit on the
     third is what this word exists to correct, and no clock shows it — a
     waiting seat's elapsed grows exactly like a working one's.
   - `idle` — its window has closed while the round has not, so the seat is free
     for more work rather than finished with the phase.
   - `done` — the stretch is over. Every row but the last of a live round.

   A cell with a role and no time or state is a seat that recorded no pass; the
   board knew its role, the ledger never saw it. A further row opens whenever any
   seat changes role, so the reader watches `impl / impl / test` become
   `review / review / review`. The first row of a round is its opening. The
   lines under the table lead with the same label and the slot in parentheses —
   `- **Agent 2** (test) …` — then the agent sitting in that seat and the last
   thing it said: the seat's own words, never the launcher's. A lone reviewer
   between rounds sits in the `Agent 3` column on a row of its own.

   The table carries the phase's **last three stages**, not all of them. An
   `Earlier:` line above it names what the cap left out, and the phase began
   there, not at the table's first row — never describe a phase as newly opened
   because its earlier rounds are off the table. When the user asks about work
   that line covers, answer from `timeline`, which still renders every pass.
7. Add two or three ordinary-English sentences under <UserFacingText/>: open
   with what this phase gives the person using the tool, then its movement and
   what remains. One topic per sentence, no more than two clauses; when a topic
   holds more than two items, give the count and what they have in common.
   When the opening is not the default, or a seat is doing something other than
   its name, say so in those words: "the review seat is writing the catalyst
   side".

   Not this:

   ```text
   The repair pass has landed its production changes: an edge whose successor
   has already ended now reports as ended rather than still waiting on its
   predecessor, ancestry questions are narrowed to the reservations that
   actually have something ordered after them, and an unrelated missing commit
   no longer poisons a whole batch of ancestry answers.
   ```

   This:

   ```text
   This phase lets one worktree's work be ordered behind another's — start here
   only once that branch is done. The repair pass fixed four cases where the
   tool gave the wrong answer about whether the waiting side was still blocked.
   Tests for that ordering come next, then verification.
   ```

   Do not paste logs or filenames, or quantify work in lines, insertions, or
   file counts.

**Questions about work already finished** — how many fix passes there have been,
how long a review took, what an earlier phase ran — are answered by

`python3 ~/.claude/scripts/delegate/progress_history.py timeline --session-dir "${SESSION_DIR}" [--phase <id>]`

which renders one row per pass, with the agent that ran each, for one phase or
the whole run. Read the answer from it rather than counting from memory or
grepping the event stream.
</ProgressReport>
