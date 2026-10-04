# Dual review

Read at the point of use from `/unit:delegate`. Defines `<DualReview/>`,
`<TeamReview/>`, `<ReviewPromptContract/>`, `<BroadReviewPrompt/>`, and
`<ClosureReview/>` in full.

**Read when:** writing any review prompt — after an implementation or fix
completes, or when `<EarlyReviewArm/>` arms a reviewer.

<TeamReview>
A phase's broad review runs **two reviewers at once**, never one — and never
two readings of the same question, which buys one opinion twice. Each takes a
**lens**, disjoint and named in its prompt; the main review per <DualReview/>
is the third reader:

| Lens | Reads for | Seat |
| --- | --- | --- |
| `adversary` | the failing case — the input that violates a stated invariant, the caller that was not updated, the state the new code cannot reach, the test that passes for the wrong reason | `test` |
| `contract` | what the Work Order says, including every part no test covers, and anything built that it never asked for; then what the change reaches without naming — callers, consumers, public API, traits, registration, plugin wiring, invariants and transitions | `impl` |

The `craft` lens is off since 2026-10-04 (user: review trial ended); the run's
style review still audits style.

No reviewer wrote any of the code — each is a fresh session per
<ReviewPromptContract/> — so no lens has to be kept away from its own work and
no assignment turns on who wrote what. **The seat is an address, not a
judgment**: it names the column the
reviewer occupies in the progress table and the suffix on its files, and
`review.sh` derives it from the lens — so the lens is the only thing a call site
chooses, and the two can never disagree.

The adversary's brief is to break the change, not to check it. It reports the
concrete failing case it built, or reports plainly that it could not build one:
"no failing case found" from a reader who was trying is evidence, and it is the
only reading here that produces evidence by finding nothing.

Findings come back as one `review_findings_<pass>_<lens>.txt` per lens. A
read-only session cannot post to the board, so that file is the whole record —
<Synthesize/> reads both, tags each finding with the lens that caught it, and
can tell two independent findings from one finding found twice.

**A repair is not reviewed this way.** <ClosureReview/> is one reader over one
batch of ids, and splitting a path-limited read two ways buys two passes over
the same few hunks.
</TeamReview>

<ReviewPromptContract>
Every reviewer is a fresh read-only session. It narrates each activity as a
short output line so the wrapper heartbeat remains live. It does not receive a
heartbeat path and never receives `${IMPL_SUMMARY}`.

Do not repeat the implementer's listed verification or audit style. A reviewer
may run one specific omitted check only when a plausible regression lies outside
the listed gate, and must name that command. Broad reviews apply
<TypeDesignContract/>; closure reviews apply only <ClosureReview/>.

A broad review's prompt names its lens per <TeamReview/> and asks that lens's
questions and no others; a closure review names no lens.
</ReviewPromptContract>

<BroadReviewPrompt>
Write one prompt per lens at
`${SESSION_DIR}/review_prompt_${REVIEW_PASS}_<lens>.md`, each under
<ReviewPromptContract/> with:

```
You are independently reviewing a change you did not write. Read surrounding
code as needed. Report a numbered list; each item has a title, file:line,
1-3 sentence explanation, and severity:
- blocker: wrong behavior, spec violation, or missing required work
- minor: real edge, error-handling, or quality defect
- nit: non-behavioral quality; style-guide conformance is out of scope
End with APPROVE, APPROVE WITH FIXES, or REQUEST CHANGES. Do not invent findings.

## Your lens
[the lens name and what it reads for, from <TeamReview/>]

Another reviewer is reading this same diff under the other lens. Report only
what yours covers: a finding another lens owns is that lens's to
make, and duplicating it costs the synthesis a second opinion and buys a
count instead.

## Three Gods
[the section of that name in <WritePromptContract/>, verbatim]

## Specification
[implementer's Work Specification verbatim]

## Type Design Contract
[verbatim contract]

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

Stage 2 — fire: poll for <ready sentinel path> (e.g. `test -e`, sleeping ~15s
between checks), narrating each wait as one short output line. When it
appears, read <final diff path> — it supersedes the partial snapshot — and
review it in full, concentrating on hunks that changed since the snapshot.
Reconcile your provisional findings against the final diff; drop any the final
code resolves. Then answer the Review Questions and emit the normal numbered
findings and verdict from the final diff only. If the sentinel has not
appeared after 30 minutes, report that timeout and exit with an error instead
of reviewing the partial diff.
```
</BroadReviewPrompt>

<DualReview>
1. If `EARLY_REVIEW=launched`, the `adversary` is already armed and
   `${REVIEW_PASS}` already incremented: apply <ReviewDiffContract/> to the
   completed tree, deliver the final diff and ready sentinel per
   <EarlyReviewArm/>, reset `EARLY_REVIEW=none`, and keep
   `${REVIEW_DISPATCH_HANDLE}` as that lens's handle. Otherwise increment
   `${REVIEW_PASS}`. Pass 1 is the phase's broad review and runs the two
   lenses of <TeamReview/> over <BroadReviewPrompt/>; later passes are one
   reviewer over one repair, per <ClosureReview/>.
2. Apply <ReviewDiffContract/> and create every prompt the pass needs.
3. Launch each reviewer under <DispatchContract/>, pass 1's in one message so
   they run concurrently — `contract` alone when the adversary was armed early,
   both otherwise:

   ```sh
   bash ~/.claude/scripts/delegate/review.sh "${SESSION_DIR}" "${WORKING_DIR}" \
     "${SESSION_DIR}/review_prompt_${REVIEW_PASS}_<lens>.md" review \
     "<responsibility>" "<activity>" "${REVIEW_PASS}" <lens>
   ```

   A closure review passes no lens and keeps the unsuffixed artifacts:

   ```sh
   bash ~/.claude/scripts/delegate/review.sh "${SESSION_DIR}" "${WORKING_DIR}" \
     "${SESSION_DIR}/review_prompt_${REVIEW_PASS}.md" review \
     "<responsibility>" "<activity>" "${REVIEW_PASS}"
   ```

   Keep every blind-review handle; the run's one notifier instance covers the
   whole Claude set, and the Codex poll covers its set.
4. While they run, perform the main review. Pass 1 reads changed code in risk
   order: Work Order paths, public API/traits/registration/plugin wiring, then
   remaining hunks. Verify spec, extras, codebase fit, <TypeDesignContract/>, and
   `${IMPL_SUMMARY}` claims without loading or auditing the style guide. The
   summaries' self-reports per <WritePromptContract/> — what a slot was unsure
   about, what it could not verify, what it touched outside its file set — are
   this read's alone: no lens receives a summary, so a stated doubt is a place
   only the main review can go looking. Say where this read did not reach. Later
   passes read only repair paths and affected callers, consumers, transitions,
   or invariants.
5. If this main pass confirms a substantial, unambiguous spec-defined defect
   while blind reviewers remain active, read each log once, cancel them all, and
   close each one's own pass with
   `PLAN_DELEGATE_TEAM_ROLE=<seat> … finish-pass --status canceled
   --orphaned-launcher` per <PassOwnership/>, the seat from <TeamReview/>'s
   table, then open and gate the finding and apply <FixDispatch/> with useful
   partial-review evidence. Preempt at most once per phase; never edit while an
   old-diff review remains active. A canceled review supplies no verdict or
   direct-fix agreement.
6. Otherwise finish the main pass and let <DispatchContract/> await every blind
   reviewer by its host-specific path. Each `reviewed` loads its own
   `review_findings_${REVIEW_PASS}_<lens>.txt` — a closure review, its unnumbered
   symlink — into `${AGENT_REVIEW}`. A lens that reports `error` is named in one
   line and the pass continues on the readings that landed, so the synthesis
   records which lens is missing rather than presenting one as two. Numbered
   artifacts remain available.
</DualReview>

<ClosureReview>
Run `findings.py status` and write a prompt under <ReviewPromptContract/> that
contains only:

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

Forbid whole-phase and already-reviewed design findings, and style or polish
findings on code the repair did not write. An outside-path problem is valid
only when a quoted repair hunk causes it. Omit the broad questions and Type
Design Contract.

**Early form** (an <EarlyReviewArm/> launch only): the diff section holds the
partial repair diff at launch, and the prompt opens with the same
`## Implementation status` staging block as <BroadReviewPrompt/>'s early form —
arm on the open ids, their surrounding code, and the partial diff; fire on the
final path-limited diff when the ready sentinel appears; answer the questions
from the final diff only.

After the main pass agrees, record `accepted` for fixed, `still_open` for not
fixed/unclear, or `reopened` with the invalidating hunk. Open any new defect
introduced by the repair, then return to <Synthesize/>.
</ClosureReview>
