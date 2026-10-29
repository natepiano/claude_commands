# Dual review

Read at the point of use from `/unit:direct`. Defines `<DualReview/>`,
`<TeamReview/>`, `<ReviewPromptContract/>`, `<BroadReviewPrompt/>`, and
`<ClosureReview/>` in full.

**Read when:** writing any review prompt — after an implementation or fix
completes, or when `<EarlyReviewArm/>` arms a reviewer.

<TeamReview>
A phase's broad review runs `adversary` and `contract` at once, plus `rerun`
when <DualReview/> arms it. Each takes a distinct **lens**, named in its prompt;
the unit director also performs the main review per <DualReview/>:

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

The `craft` lens is off since 2026-10-04 (user: review trial ended); the unit
director's main review audits style each phase.

When the production doc names a review pool, the launcher allocates each
lens's reviewer from the unit's own; the worker choice and role reassignment
below do not apply.

Otherwise each lens gets a different open worker, and an idle open worker
always takes a lens before a new one opens. Writers count: a worker that wrote
the phase is still a reviewer. Among the idle workers, give a lens first to one
that did not write the files it reviews. When none is left, a writer takes it;
on the `adversary` lens it argues against its own choices. Move each worker
under <RoleReassignment/>. Open a role-named review worker only when every open
worker is busy, and keep it for reuse.

The worker is the address and the lens is the judgment. Pass both to `review.sh`;
findings remain keyed by lens even when a different slot takes it, and each
worker's agent log carries its slot (`review_agent_<pass>_<lens>-<slot>.log`).

The adversary's brief is to break the change, not to check it. It reports the
concrete failing case it built, or reports plainly that it could not build one:
"no failing case found" from a reader who was trying is evidence. The same rule
applies to the `rerun` reviewer's attempt to build a rerun case.

Findings come back as one `review_findings_<pass>_<lens>.txt` per lens, in
`${PHASE_DIR}` because every launch names its phase with `--phase`: two phases
reviewing at once never share a pass's files. That file is the whole review
result: <Synthesize/> reads every selected lens's file, including `rerun` when
armed, tags each finding with its lens, and can tell two independent findings
from one finding found twice.

**A repair is not reviewed this way.** <ClosureReview/> is one reader over one
batch of ids, and splitting a path-limited read two ways buys two passes over
the same few hunks.
</TeamReview>

<ReviewPromptContract>
When the production doc names a review pool, every code review prompt opens
with:

```
You are a pooled reviewer for <unit>, phase <N>. Edit nothing. Your first command is git -C <worktree> rev-parse HEAD; if it is not <head>, report error. Re-read the tree; trust nothing from memory about its state.
```

Otherwise every review prompt opens with the selected worker's role switch:

```
You are slot <slot>, now working as a reviewer. Edit nothing. Read the diff
cold as if someone else wrote it. If you wrote any of the code, argue against
your own choices as an adversary would.
```

In a run's own-worker review, the opening also gives the concrete
`board.sh role ... review` command for the worker's first tool call. The reviewer
narrates each change of activity as a short output line so the wrapper
heartbeat remains live, and never polls, sleeps or narrates a wait. It receives no
heartbeat path and never receives `${IMPL_SUMMARY}`. `review.sh` verifies that
the working tree did not change.

Do not repeat the implementer's listed verification or audit style. A reviewer
may run one specific omitted check only when a plausible regression lies
outside the listed gate, and must name that command; a Codex reviewer runs it
with `verify.sh` per the `## Builds and tests` section of
<WritePromptContract/> item 3, which every Codex review prompt carries verbatim. Broad reviews apply
<TypeDesignContract/>; closure reviews apply only <ClosureReview/>.

For a pool review, the Three Gods and Type Design Contract sections each hold
the one line `As in your standing instructions.` and name no file; this also
applies to the broad and closure prompt forms below. Otherwise every review
prompt carries the inline Three Gods sentence and the Type Design Contract: verbatim
for a new worker, or by <WritePromptContract/> item 7's pointer for a follow-up.

A broad review's prompt names its lens per <TeamReview/> and asks that lens's
questions and no others; a closure review names no lens.

Every code review prompt, under any lens and in a closure review, also asks
this, verbatim:

```
List each existing assertion or test that the diff removes, weakens, changes
(same assertion, other expected value) or turns off (ignore, skip, xfail), with
its path, test name, old text and new text. If there is none, write "none".
Each one that the work order does not name in an "Assertion-Change:" line is a
finding.
```

`assertion_guard.py` finds the same list mechanically, and it blocks lint and
the checkpoint; the reviewer judges whether a ruled change is correct. In
cargo-liner-tile checkpoint e21d5b93, a weakened `assert_eq!` in a 6,000-line
diff passed every review (user, 2026-10-28).
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
- nit: non-behavioral quality; style-guide conformance is out of scope
End with APPROVE, APPROVE WITH FIXES, or REQUEST CHANGES. Do not invent findings.

## Your lens
[the lens name and what it reads for, from <TeamReview/>]

Other reviewers read this same diff under their own lenses. Report only
what yours covers: a finding another lens owns is that lens's to
make, and duplicating it costs the synthesis a second opinion and buys a
count instead.

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
   completed tree, keep the final diff and ready sentinel for step 3, and keep
   `${REVIEW_DISPATCH_HANDLE}` as that lens's handle. Otherwise increment
   `${REVIEW_PASS}`. Pass 1 is the phase's broad review and runs the selected
   lenses of <TeamReview/> over <BroadReviewPrompt/>; later passes are one
   reviewer over one repair, per <ClosureReview/>.
2. Apply <ReviewDiffContract/>. Before the broad review, save the complete phase
   diff to a file and run this read-only check in every repository, including Rust:

   ```sh
   python3 ~/.claude/scripts/direct/rerun_lens.py arm --diff <diff>
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
   In a production whose doc names a review pool, create each prompt with
   <ReviewPromptContract/>'s pooled opening. Otherwise choose each lens's open
   worker under <TeamReview/>, apply <RoleReassignment/> when its role changes,
   and create the prompt with that slot's role-switch opening.
3. Launch each reviewer under <DispatchContract/>, pass 1's in one message so
   they run concurrently — `contract` and, when armed, `rerun` if the adversary
   was armed early; otherwise `adversary`, `contract`, and `rerun` when armed.
   If `EARLY_REVIEW=launched`, deliver its final diff and ready sentinel in that
   message per <EarlyReviewArm/> so `review.sh` sends it Stage 2; then reset
   `EARLY_REVIEW=none`. Do not launch a second adversary.
   The `rerun` result is `${PHASE_DIR}/review_findings_${REVIEW_PASS}_rerun.txt`.
   When the production doc names a review pool:

   ```sh
   bash ~/.claude/scripts/direct/review.sh --pool code \
     --lease "<slug>/<unit>/<phase>" \
     --phase "<phase id>" "${SESSION_DIR}" "${WORKING_DIR}" \
     "${PHASE_DIR}/review_prompt_${REVIEW_PASS}_<lens>.md" review \
     "<responsibility>" "<activity>" "${REVIEW_PASS}" <lens>
   ```

   A pool closure uses `review_prompt_${REVIEW_PASS}.md` and omits the lens.
   Otherwise, for an open worker:

   ```sh
   bash ~/.claude/scripts/direct/review.sh --to "<full-worker-name>" \
     --phase "<phase id>" "${SESSION_DIR}" "${WORKING_DIR}" \
     "${PHASE_DIR}/review_prompt_${REVIEW_PASS}_<lens>.md" review \
     "<responsibility>" "<activity>" "${REVIEW_PASS}" <lens>
   ```

   A closure review passes no lens and keeps the unsuffixed artifacts:

   ```sh
   bash ~/.claude/scripts/direct/review.sh --to "<full-worker-name>" \
     --phase "<phase id>" "${SESSION_DIR}" "${WORKING_DIR}" \
     "${PHASE_DIR}/review_prompt_${REVIEW_PASS}.md" review \
     "<responsibility>" "<activity>" "${REVIEW_PASS}"
   ```

   If every open worker is busy, choose an unused role-named slot (`review`, then
   `review2`) and open and retain it; for example:

   ```sh
   bash ~/.claude/scripts/direct/review.sh --slot review \
     --phase "<phase id>" "${SESSION_DIR}" "${WORKING_DIR}" \
     "${PHASE_DIR}/review_prompt_${REVIEW_PASS}_<lens>.md" review \
     "<responsibility>" "<activity>" "${REVIEW_PASS}" <lens>
   ```

   Only a review outside a run calls `review.sh` with neither `--to` nor
   `--slot`.

   Keep every blind-review handle; the run's one notifier instance covers the
   whole Claude set, and the Codex poll covers its set.
4. While they run, perform the main review. Pass 1 reads changed code in risk
   order: Work Order paths, public API/traits/registration/plugin wiring, then
   remaining hunks. Verify spec, extras, codebase fit, <TypeDesignContract/>, and
   `${IMPL_SUMMARY}` claims. On pass 1 only, run the style review named by
   <PhaseChecks/>: read `~/.claude/commands/unit/style_review.md` in full and
   apply <StyleReview/> with `since <commit from ${PHASE_DIR}/phase_baseline>`
   and `${SESSION_DIR}` as its directory; supply the phase's findings ledger.
   A missing or invalid baseline stops the review. Save its table and result to
   `${SESSION_DIR}/phase_style_review_<phase-id>.md`, including `Off` or an
   error. For its ledger writes use `findings.py open --caught-by main` with
   each violation's severity, title, file and line; retain the returned ids
   for <Synthesize/> without opening them again. A phase with no Rust change runs only the
   banned-words scan under <StyleReview/>. Blind reviewers never audit style.
   The summaries' self-reports per <WritePromptContract/> — what a slot was unsure
   about, what it could not verify, what it touched outside its file set — are
   this read's alone: no lens receives a summary, so a stated doubt is a place
   only the main review can go looking. Say where this read did not reach. Later
   passes read only repair paths and affected callers, consumers, transitions,
   or invariants; do not repeat the checklist walk.
5. If this main pass confirms a substantial, unambiguous spec-defined defect
   while blind reviewers remain active, read each log once, cancel them all, and
   close each one's own pass with
   `UNIT_TEAM_ROLE=<worker> … finish-pass --status canceled
   --orphaned-launcher` per <PassOwnership/>, using each selected review slot
   unless the production doc names a review pool. With a pool, read each
   review's slot from the one line in `${PHASE_DIR}/review_slot_<lens>`;
   for a closure, use `${PHASE_DIR}/review_slot`. A missing file means that
   launcher opened no pass, so skip its cancel call. Then open and gate the
   finding and apply <FixDispatch/> with useful
   partial-review evidence. Preempt at most once per phase; never edit while an
   old-diff review remains active. A canceled review supplies no verdict or
   direct-fix agreement.
6. Otherwise finish the main pass and let <DispatchContract/> await every blind
   reviewer by its host-specific path. Each `reviewed` loads its own
   `${PHASE_DIR}/review_findings_${REVIEW_PASS}_<lens>.txt` — a closure review, its unnumbered
   symlink — into `${AGENT_REVIEW}`. A lens that reports `error` is named in one
   line and the pass continues on the readings that landed, so the synthesis
   records which lens is missing rather than counting it as a completed reading.
   Numbered artifacts remain available.
</DualReview>

<ClosureReview>
Run `findings.py status` and write a prompt under <ReviewPromptContract/> that
contains only:

- The role-switch opening from <ReviewPromptContract/>.
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

Forbid whole-phase and already-reviewed design findings, and style or polish
findings on code the repair did not write. An outside-path problem is valid
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
