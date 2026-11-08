# Dual review

Read at the point of use from `/unit:direct`. Defines `<DualReview/>`,
`<TeamReview/>`, `<ReviewPromptContract/>`, `<BroadReviewPrompt/>`, and
`<ClosureReview/>` in full.

**Read when:** writing any review prompt — after an implementation or fix
completes, or when `<EarlyReviewArm/>` arms a reviewer.

<TeamReview>
A phase's broad review runs `adversary` and `contract` together.
It also runs `rerun` when <DualReview/> arms it.
Each prompt names its lens. The director performs the main review under <DualReview/>.

| Lens | Reads for |
| --- | --- |
| `adversary` | the failing case — the input that violates a stated invariant, the caller that was not updated, the state the new code cannot reach, the test that passes for the wrong reason |
| `contract` | what the Work Order says, including every part no test covers, and anything built that it never asked for; then what the change reaches without naming — callers, consumers, public API, traits, registration, plugin wiring, invariants and transitions; and the repository's targets file (`docs/hana/targets.md` in hana), when present |
| `rerun` | whether interrupted or failed work can finish when run again; lasting records, marks, locks, claims, state files, moved or deleted files, and lists of steps where one failure can stop later steps |

The `rerun` reviewer asks these questions for each lasting write or step list:

1. If the command stops at any line and runs again, does it finish?
   Case: Restart of claude3 never ran after one account failed.
2. Does it trust a stored status without checking the live thing: pid and
   process start time, path, or lock holder?
   Case: `down` trusted a "stopped" record for a session that was live.
3. Does a failure move or delete what a rerun needs?
   Case: A failed restore moved the records to `history/`, so the rerun had
   nothing to restore.
4. Does one failure stop other, unrelated steps, such as `set -e` over a list?
   Case: `set -e` in `herdr-up.sh` stopped every later account at the first
   failed one.
5. Does each mark have an expiry or an owner check, and a command that clears it?
   Case: `launch_pending` and the "restarting" claim had no command to clear them.

An answer that shows unsafe rerun behavior is a defect, fixed in the phase
as a blocker or minor finding. The prompt closes by asking for the rerun case
the reviewer built, or a plain statement that it could not build one.

The `craft` lens remains off since 2026-10-04 (user: review trial ended).
The `contract` reviewer also owns the phase style review under
<ReviewPromptContract/>.

The Codex lenses use the unit's read-only workers.
Dispatch with `assign.sh --role review` under <LongLivedWorkers/>.
The worker command selects a free reviewer and retains its native conversation across phases.
Writers keep their assigned roles.

The worker is the address and the lens is the judgment. Pass the lens to
`assign.sh`; its returned handle names the selected worker and summary. Each
lens has its own artifact paths.

The adversary's brief is to break the change, not to check it. It reports the
concrete failing case it built, or reports plainly that it could not build one:
"no failing case found" from a reader who was trying is evidence. The same rule
applies to the `rerun` reviewer's attempt to build a rerun case.

Findings come back in each dispatch's summary under `${PHASE_DIR}`. Every
launch names its phase, lens and pass, so concurrent reviews keep separate
files. The summary is the whole review result: <Synthesize/> reads every
selected lens's summary, including `rerun` when armed, tags each finding
with the lens that caught it, and can tell two independent findings from
one finding found twice.

**A repair is not reviewed this way.** <ClosureReview/> is one reader over one
batch of ids, and splitting a path-limited read two ways buys two passes over
the same few hunks.
</TeamReview>

<ReviewPromptContract>
Every Codex review prompt opens with:

```
You are the unit's code reviewer for <unit>, phase <N>. Edit nothing. Read
config/review/code.md at your first review and when it changes. Your first
command is git -C <worktree> rev-parse HEAD; if it is not <head>, report error.
Re-read the tree; trust nothing from memory about its state.
```

For `herdr`, the reviewer posts no board lines and runs no suites.
The director or a writer handles required board posts and suites.
The reviewer reports each activity change as a short output line.
It never polls, sleeps, or reports a wait.
Give it no heartbeat path or `${IMPL_SUMMARY}`.
The reviewer runs read-only.

Do not repeat the implementer's listed verification.
For `herdr`, name an omitted check for the director or a writer when a plausible regression requires it.
It names the command and uses `verify.sh` under <WritePromptContract/> item 3.
Every Codex review prompt carries that item's `## Builds and tests` section verbatim.
Broad reviews apply <TypeDesignContract/>.
Closure reviews apply <ClosureReview/>, including any owed style assignment.

Every review prompt carries the Three Gods sentence and the Type Design
Contract under <WritePromptContract/>. Its standing-instructions line names
`config/review/code.md`.

A broad review's prompt names its lens per <TeamReview/> and asks that lens's
questions, plus its style assignment below; a closure review names no lens.

The phase style review starts **owed**. While owed, dispatch the `contract`
reviewer through `assign.sh --role review --style-since <phase_baseline commit>`
with the normal phase, run, worktree and prompt options below. The director
passes the option, never assignment text. The runner prepares fresh input
from that baseline through the working tree, including untracked files. To
pin a committed range and exclude other work in flight, also pass
`--style-until <end commit>`; both commits are validated before dispatch.
The runner saves input under the phase's `style/` folder,
appends `config/review/style.md` once and
saves the returned marked report as `${PHASE_DIR}/phase_style_review.md`.
Its `style_review_state.json` records **complete** or **owed** and the input
and report paths. Earlier pass inputs and reports remain in their directories.
The reviewer stays read-only: preparation and report saving belong to the
runner. See <StyleReview/> for the manual caller of the same helper.

The assignment becomes **complete** only when a reviewer returns the report
with a finished result (findings or Pass) and includes its findings in the
normal review summary for <Synthesize/>. The runner completes Off or No
changes to review without sending an assignment. A canceled or failed dispatch,
missing markers, invalid baseline or errored report leaves it owed, even if an
artifact was written. Preparation errors do not stop the code review.

For an owed retry, use the same `--style-since` option on the next closure
`assign.sh --role review` dispatch, with `--to` naming the same reviewer; it receives fresh input
and retains its completed guide load. Keep `--style-until` too when reviewing
a pinned range. The runner refuses a second style dispatch while the phase
has a live owner, naming that dispatch; cancel or finish it before retrying.
A dead launcher releases ownership on the next dispatch.
Once complete, later reviewers check style repairs through finding ids
without repeating the pass. The phase cannot converge while style is owed;
if no repair needs closure, dispatch that reviewer with the owed assignment
alone. The director reads the ledger and result for <RunSummary/> and never
substitutes a guide load or rule walk. Early reviews cannot carry this option:
style input requires the final phase diff.
Use `assign.sh` for every review dispatch.

Every code review prompt, under any lens and in a closure review, also asks
this, verbatim:

```
List each existing assertion or test that the diff removes, weakens, changes
(same assertion, other expected value) or turns off (ignore, skip, xfail), with
its path, test name, old text and new text. If there is none, write "none".
Each one that the work order does not call for is a finding.
```

In cargo-liner-tile checkpoint e21d5b93, a weakened `assert_eq!` in a
6,000-line diff passed every review (user, 2026-10-28).
</ReviewPromptContract>

<BroadReviewPrompt>
Write one prompt per lens at
`${PHASE_DIR}/review_prompt_${REVIEW_PASS}_<lens>.md`, each under
<ReviewPromptContract/> with:

```
[the role-switch opening from <ReviewPromptContract/>]

Read surrounding code as needed. Report a numbered list; each item has a title, file:line,
1-3 sentence explanation, and severity:
- blocker: wrong behavior, spec violation, or missing required work
- minor: real edge, error-handling, or quality defect
- nit: non-behavioral quality
End with APPROVE, APPROVE WITH FIXES, or REQUEST CHANGES. Do not invent findings.

## Your lens
[the lens name and what it reads for, from <TeamReview/>]

Other reviewers read this same diff under their own lenses. Report only
what yours covers, including your assigned phase style review: a finding
another lens owns is that lens's to make, and duplicating it costs the
synthesis a second opinion and buys a count instead.

## Three Gods
[the section of that name in <WritePromptContract/>, verbatim]

## Builds and tests
[the section of that name in <WritePromptContract/> item 3, verbatim; Codex reviewers only]

## Specification
[implementer's Work Specification verbatim]

## Type Design Contract
[verbatim contract or the allowed follow-up pointer]

## Diff
[complete diff]

## Review Questions
[this lens's questions, below, and no others]
```

The questions are the lens, so each prompt carries only its own:

- `adversary` — 1. What input, state, or ordering makes this change do the wrong
  thing? 2. Which caller or consumer of a changed symbol was left behind?
  3. Which test passes for a reason other than the behavior it names? Close with
  the failing case you constructed, or with a plain statement that you could not
  construct one.
- `contract` — 1. Complete and correct against the specification? 2. Anything
  implemented that it did not ask for? 3. What does the change reach without
  naming — callers, consumers, public API, traits, registration, plugin wiring
  — and what there still assumes an invariant or state transition it changed?
  4. Consistent with surrounding code? 5. Are domain types clear, and are owned
  bare Option<T> values replaced or justified at an external boundary?
  6. Does it meet every target in the repository's targets file, when present?
  A miss is a defect.
- `rerun` — Copy the five questions and their real cases from <TeamReview/>
  verbatim. Apply them to each record, mark, lock, claim, state file, moved or
  deleted file, and list of steps the change writes or runs. Treat unsafe rerun
  behavior as a blocker or minor defect to fix in this phase. Close with the
  rerun case you built, or a plain statement that you could not build one.

**Early form** (an <EarlyReviewArm/> launch, which arms the `adversary` alone):
the `## Diff` section holds the partial diff at launch, and an
`## Implementation status` section is inserted before it:

```
## Implementation status

Implementation is estimated ~N% complete and still running; the diff below is
a partial snapshot. Work in two stages.

Stage 1 — arm, now: read the specification, the partial diff, and the
surrounding code, callers, and consumers it touches. Draft provisional
findings. Emit no verdict.

Then end your turn: your final answer is the provisional findings, headed
`Stage 1 — provisional, no verdict`. Never poll for the final diff or sleep.

Stage 2 — fire: when the final diff is ready, a message naming
<final diff path> arrives as your next turn. Read it — it
supersedes the partial snapshot — and review it in full, concentrating on
hunks that changed since the snapshot. Reconcile your provisional findings
against the final diff; drop any the final code resolves. Then answer the
Review Questions and emit the normal numbered findings and verdict from the
final diff only.
```
</BroadReviewPrompt>

<DualReview>
1. If `EARLY_REVIEW=launched`, the `adversary` is already armed and
   `${REVIEW_PASS}` already incremented: apply <ReviewDiffContract/> to the
   completed tree, deliver the final diff and ready sentinel per
   <EarlyReviewArm/> (the assign dispatch then sends that reviewer its Stage 2),
   reset `EARLY_REVIEW=none`, and keep `${REVIEW_DISPATCH_HANDLE}` as that
   lens's handle. Otherwise increment
   `${REVIEW_PASS}`. Pass 1 is the phase's broad review and runs the selected
   lenses of <TeamReview/> over <BroadReviewPrompt/>; later passes are one
   reviewer over one repair, per <ClosureReview/>.
   For `herdr`, Stage 2 uses `worker.py` with dispatch id `<dispatch id>-2`.
   The launcher reads the transcript before input and requires `DONE <dispatch id>-2` to complete review.
   Review Stage 2 reports through its status and summary files.
2. Apply <ReviewDiffContract/>. Before the broad review, save the complete phase
   diff to a file and run this read-only check in every repository, including Rust:

   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/direct/rerun_lens.py arm --diff <diff>
   ```

   `<diff>` is that saved diff, including new phase files, made with whole-file
   context (`git diff --unified=100000`), so a distant `set -e` and the loop it
   covers are both in it. Arm `rerun` if either
   the Work Order's `**State:**` names lasting state or this command prints
   `armed`. `none` and an absent State line do not arm it; they never override
   a diff hit. Lasting state includes a record, mark, lock, claim, state file,
   file move or delete, or a list of steps where one step can fail. The check
   reports each hit as `path:line — <kind>`; writes in tests, docs and fixtures
   do not count. Pure computation and docs-only changes skip `rerun` when State
   names no lasting state and the check prints `not armed`. A check error is
   not `not armed`: resolve it before launching the broad review.

   Use the completed diff for this decision even when `adversary` was armed
   early. All broad reviewers read the same review copy and final diff.
   Create each lens's prompt under <ReviewPromptContract/>.
3. Launch each reviewer under <DispatchContract/>.
   Launch pass 1's `adversary`, `contract`, and armed `rerun` lenses in one message.
   When the adversary is already armed, launch the other selected lenses together.
   Each call uses:

   ```sh
   bash ~/.claude/scripts/direct/assign.sh --role review --lens <lens> \
     --pass "${REVIEW_PASS}" --phase "<phase id>" \
     --session-dir "${SESSION_DIR}" --cwd "${WORKING_DIR}" \
     --prompt "${PHASE_DIR}/review_prompt_${REVIEW_PASS}_<lens>.md"
   ```

   For `herdr`, name `--to review2` for a second lens that runs at the same time.
   The worker command refuses automatic selection when all suitable workers are busy.
   Give each additional concurrent lens a distinct available review name with `--to`.
   A closure review omits `--lens` and uses `review_prompt_${REVIEW_PASS}.md`.
   Keep each dispatch handle and apply <LongLivedWorkers/> for selection and reuse.
   Read the summary path each dispatch returns.
4. While they run, perform the main review. Pass 1 reads changed code in risk
   order: Work Order paths, public API/traits/registration/plugin wiring, then
   remaining hunks. Verify spec, extras, codebase fit, <TypeDesignContract/>, and
   `${IMPL_SUMMARY}` claims. Style belongs to the assigned reviewer under
   <ReviewPromptContract/>; the director does not load or walk the guide.
   The summaries' self-reports per <WritePromptContract/> — what a slot was unsure
   about, what it could not verify, what it touched outside its file set — are
   this read's alone: no lens receives a summary, so a stated doubt is a place
   only the main review can go looking. Say where this read did not reach. Later
   passes read only repair paths and affected callers, consumers, transitions,
   or invariants.
5. If this main pass confirms a substantial, unambiguous spec-defined defect
   while blind reviewers remain active, read each log once, cancel them all, and
   close each one's own pass with
   `UNIT_TEAM_ROLE=<worker> … finish-pass --status canceled
   --orphaned-launcher` per <PassOwnership/>, using each selected review slot.
   A missing slot means that launcher opened no pass; skip its cancel call.
   Then open and gate the
   finding and apply <FixDispatch/> with useful
   partial-review evidence. Preempt at most once per phase; never edit while an
   old-diff review remains active. A canceled review supplies no verdict or
   direct-fix agreement. Any owed style assignment follows
   <ReviewPromptContract/> on the next reviewer dispatch.
6. Otherwise finish the main pass and let <DispatchContract/> await every blind
   reviewer by its host-specific path. Each completed dispatch loads the summary
   named by its handle into `${AGENT_REVIEW}`. A lens that reports `error` is named in one
   line and the pass continues on the readings that landed, so the synthesis
   records which lens is missing rather than counting it as a completed reading.
   Numbered artifacts remain available.
</DualReview>

<ClosureReview>
Run `findings.py status` and write a prompt under <ReviewPromptContract/> that
contains only:

- The role-switch opening from <ReviewPromptContract/>.
- The style assignment from <ReviewPromptContract/> when still owed, with the
  complete phase diff and baseline; its scope is separate from the repair diff.
- Each open id, severity, file:line, and title verbatim.
- Paths named by the fix plus new post-fix paths.
- Diff limited to those paths, including new files.
- Three questions: for each id, `FIXED`, `NOT FIXED`, or `UNCLEAR` with
  file/line evidence; for each id, the regression test that covers it and
  whether it would fail without the repair — a testable id with no such test is
  `NOT FIXED`; and whether this repair breaks any caller, consumer, transition,
  or invariant of its changed symbols.
- The `## Three Gods` section of <WritePromptContract/>, verbatim; a repair
  hunk that breaks one is a finding of the repair.
- For a Codex reviewer, the `## Builds and tests` section of
  <WritePromptContract/> item 3, verbatim.
- The `## Type Design Contract` section using <WritePromptContract/> item 7's
  follow-up pointer.

Outside an owed style assignment, forbid whole-phase and already-reviewed
design findings, and style or polish findings on code the repair did not write.
An outside-path problem is valid
only when a quoted repair hunk causes it. Omit the broad questions and Type
Design questions.

**Early form** (an <EarlyReviewArm/> launch only): the diff section holds the
partial repair diff at launch, and the prompt opens with the same
`## Implementation status` staging block as <BroadReviewPrompt/>'s early form —
arm on the open ids, their surrounding code, and the partial diff, then end
the turn; fire on the final path-limited diff when its message arrives;
answer the questions from the final diff only.

After the main pass agrees, record `accepted` for fixed, `still_open` for not
fixed/unclear, or `reopened` with the invalidating hunk. Open any new defect
introduced by the repair, then return to <Synthesize/>.
</ClosureReview>