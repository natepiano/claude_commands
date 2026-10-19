---
description: Show what this unit's /unit:direct run and its agents are doing right now.
argument-hint: "[tables|on|off]"
---

# Direct — status report

**Usage:** `/unit:report [tables|on|off]`

Type this to see the phase, stage, ETA, workers and summary; add `tables` for
the recorder's clocks and round tables. It runs inside
the current session and already knows the session directory, the phase, and
which dispatches are live. If no `/unit:direct` run is active, say so in one line and
stop.

`/unit:direct` reads this file at every notifier tick and Codex poll timeout.
It defines `<ProgressReport/>` — the content of an update. `<ProgressContract/>` in
`~/.claude/commands/unit/direct.md` keeps the timing rules that say when one is
owed. Never compose a report from memory of an earlier read: the byte-for-byte
copy rule and the ordinary-English closing sentences are the parts that decay.

With `on` or `off`, run
`zsh ~/.claude/scripts/delegate/unit_notifier.sh "$CLAUDE_CODE_SESSION_ID" on|off`
using the requested word. Relay its output and stop without composing a report.
A Codex unit has no notifier; say this switch is unavailable there. If the
command fails, relay its message as printed: exit 1 covers both a session with
no active run and a run whose notifier instance is missing, and the message
says which. `tables` composes the report below with `progress --tables`;
ordinary reports and notifier ticks omit that flag. Reject other arguments.

Everything below is the contract.

---

<ProgressReport>
1. Check launcher state first. For Codex, `exit_code` alone marks terminal
   completion; a returned `session_id` without it remains active. When no
   dispatch, background verification or open activity remains active, process
   completion; its result closes with the report from step 5. A closed pass
   alone is not completion (step 5). Ticks arriving during a report, or several
   together, get one report.
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
   already listed among this tick's workers.
5. Run:

   `python3 ~/.claude/scripts/delegate/progress_history.py calibrate --session-dir "${SESSION_DIR}" --candidate-percent "${PHASE_RAW_PERCENT}" --cap-stage "<stage>"`

   Pass the same stage to both calls; `progress` refuses a calibration drawn
   for another. Use its phase suggestion when applicable; otherwise keep the raw value. Then
   run:

   `python3 ~/.claude/scripts/delegate/progress_history.py progress --session-dir "${SESSION_DIR}" --project-raw-percent "${PROJECT_RAW_PERCENT}" --project-percent "${PROJECT_RAW_PERCENT}" --phase-raw-percent "${PHASE_RAW_PERCENT}" --phase-percent "${PHASE_REPORTED_PERCENT}" --cap-stage "<stage>" --activity "<current activity>" [--phase-override-reason "<specific evidence>"] [--tables]`

   Pass `--tables` only for `/unit:report tables`. The call also rewrites this
   unit's phase note, which keeps the full phase list; write nothing there by
   hand. For a requested full phase table, use `phase_table.py show
   --session-dir "${SESSION_DIR}"`.

   **Between windows, report the last recorded values.** The recorder prints
   an `as of` line and `No pass or activity is open.` without opening a window
   or changing progress. A closed pass alone does not finish a dispatch: check
   `phases/<id>/impl_status_<slot>`, board posts and launcher completion. Alive
   means keep the run going; terminal means step 1. Never open an activity just
   to render a report. An activity still open after the phase closes remains
   the stage; `No active phase to report` means no phase or activity is open.

   Include the override reason only when rejecting applicable calibration.
   Copy the recorder's phase heading, stage line, ETA line and wall clock
   exactly, keeping its `as of` and no-open-window lines when present. The ETA
   is the latest promise, including `passed` when overdue; never substitute a
   schedule prediction. For `tables`, also copy both tables, `Earlier:` and
   verification gate lines byte-for-byte. Worker narration supplies step 7;
   never paste the `- **Agent N**` lines themselves.
6. Present sections in this order: phase and stage, ETA line, worker table,
   summary sentences, then `See /showrunner:unit_board for the board.` For `tables`, put
   the two recorder tables after the ETA line. If early review is armed, say
   the writer and reviewer are working at once.

   Use the recorder's worker lines for slot, role, state, model and effort.
   `running` means working; `waiting` means held up; `idle` means its window
   closed while the round continues; `done` means its stretch ended. With
   `tables`, the round table adds timing and history for the last three stages;
   use `timeline` for stages named by `Earlier:`.
7. Write the worker table yourself: one row per worker the recorder listed, columns
   `#`, `Role`, `Model`, `Effort`, `Details`. Take the number and the slot from
   the worker's label, the model and effort from the recorder when it names them
   and `-` when it does not. `Details` is a sentence of yours, not the worker's,
   answering two things: what the worker is doing in the phase's own terms, and
   whether it is moving, waiting, or done. Build it from the worker's board lines
   since the last cursor plus the Work Order, and hold these rules:

   - Track the opening phase of the current Codex thread, not the worker name. If
     that phase is older, begin `Details` with `Opened in phase <id>;`; omit it
     when the thread opened in the current phase. A replacement thread under
     the same worker name starts fresh and holds only its own opening prompt, so
     the dispatch that creates it is an opening prompt, not a follow-up.
   - Read the full board line, never the recorder's 72-character cut. A line
     ending in `…` has lost its information and must never reach the user.
   - Name work by what it changes for the phase — the Work Order item, the
     defect being fixed, the tests being written — never by the team's own
     shorthand for it. The team's lane names, gate names, and finding IDs mean
     nothing outside the session.
   - Coordination mechanics are not news. Claims, tokens, role announcements,
     handoffs, "no-more-edits", gate reruns — report the state they imply and
     name the mechanism only when a worker is stuck on it, with how long.
   - A finding is named by the defect it describes, not by its ID or the
     reviewer's phrasing.
   - Give an age only when it is news — a worker silent long enough to look
     stalled, or finished and how long ago. "1s ago" tells the reader nothing.
   - One table per report, from this tick's recorder output only. Never carry a
     previous tick's rows forward or print two tables.

   Not this:

   ```text
   | # | Role | Model | Effort | Details |
   |---|------|-------|--------|---------|
   | 1 | impl | gpt-5.6-sol | xhigh | 1s ago · claim: token cargo acquired for up to 3600s |
   | 2 | test | gpt-5.6-sol | xhigh | Identity lane compiles with six Catalyst defects and one test fixture c… |
   ```

   This:

   ```text
   | # | Role | Model | Effort | Details |
   |---|------|-------|--------|---------|
   | 1 | impl | gpt-5.6-sol | xhigh | starting its verification build |
   | 2 | test | gpt-5.6-sol | xhigh | fixing six compile errors its new identity tests turned up |
   ```
8. Add two or three ordinary-English sentences under <UserFacingText/>: open
   with what this phase gives the person using the tool, then its movement and
   what remains. One topic per sentence, no more than two clauses; when a topic
   holds more than two items, give the count and what they have in common.
   When the opening is not the default, or a worker is doing something other than
   its name, say so in those words: "the test worker is writing the catalyst
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
