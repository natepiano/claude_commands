---
description: After implementing a phase, keep review prose temporary, update remaining Work Orders, review approved next items, and prepare the phase for as-built shrink.
---

Use this after implementing a phase of a multi-phase plan. Review prose is
ephemeral: write it only under `${SESSION_DIR}`, use it to update remaining Work
Orders and prepare as-built input, then let `/plan:delegate` delete it after the
phase checkpoint. The forward review also checks the plan's sibling
`{plan-name}-next.md` when present. Never append review prose to either file.

**Read `~/.claude/docs/type_design.md` first and follow it.** Apply its type-name
and `Option<T>` rules to the temporary retrospective, remaining-phase review, and all
Work Order revisions. Include its complete contents verbatim in the architect
subagent prompt so the reviewer receives the same contract as `/plan:delegate`.

**Delegate-ready plans.** If the plan follows `~/.claude/docs/delegate_plan_format.md` (a `## Delegation Context` section + per-phase `#### Work Order`), this command must keep every remaining phase **dispatch-ready**: learnings are folded *into* the remaining Work Orders (Spec, Files, Acceptance gate, and especially **Constraints from prior phases**), not merely appended as review notes. The test after this command runs: `/plan:delegate <plan> phase <next>` can assemble its prompt with zero codebase research. See `<MaintainWorkOrders/>` in Step 5.

**Auto mode.** If `$ARGUMENTS` contains the token `auto` (passed by the `/plan:delegate` loop), this command asks the user nothing: significant findings that survive filtering are deferred into the affected phase's Work Order as `**Pending decision:**` blocks (format: `~/.claude/docs/delegate_plan_format.md`) instead of being presented. Invocations without `auto` behave exactly as written below.

## Step 1: Locate the plan doc

The plan doc should already be in conversation context — it is the doc the just-completed phase came from.

- Strip the `auto` and `skip-architect` tokens from `$ARGUMENTS` first — they are mode switches, not paths.
- If exactly one plan doc is in scope, use it.
- If `$ARGUMENTS` names a path, use that path (overrides inference).
- If no plan doc is in scope, **ask the user** for the path before proceeding. Do not guess. This case should be rare.
- Under `/plan:delegate`, inherit its `${SESSION_DIR}` and `${WORKING_DIR}`.
  Invoked standalone, create a session with `prepare_session.sh` now so every
  temporary review artifact has an explicit owner.
- Set `${NEXT_ITEMS_PATH}` from the caller when available. Otherwise derive the
  sibling kebab-case `{plan-name}-next.md` path using `/plan:delegate`'s naming
  rule. The file is optional.

State the path you picked in one line: `Reviewing <relative/path/to/plan.md>.`

## Step 2: Identify the phase that just completed

Infer from conversation: the most recently implemented phase is the one being reviewed. State which phase in one line: `Phase under review: <phase number / title from the plan>.`

If the conversation does not make the phase obvious, ask the user one clarifying question and wait.

## Step 3: Record a temporary retrospective and mark the phase complete

1. Mark the phase complete in the plan's existing convention. For a
   delegate-ready plan, set `status: done` and keep its `#### Work Order` only
   until `/plan:shrink` replaces this phase before checkpoint. Do not edit any
   earlier `done` phase.
2. Write `${SESSION_DIR}/phase_review_retrospective_<phase>.md` using:

   ```text
   Phase <id>: <title>

   **What worked:** <one or two terse bullets>
   **What deviated from the plan:** <bullets — scope changes, approach changes, anything the plan did not predict>
   **Surprises:** <bullets — things learned during implementation that the plan author did not know>
   **Implications for remaining phases:** <bullets — concrete effects on later phases; this is the bridge into Step 4>
   **State and consequence audit:** <one disposition per new or changed state/outcome, or `None`>
   ```

   Drop empty fields. Keep bullets short and concrete. This file is input to the
   remaining-phase review and closeout shrink; it never enters the repository.

<StateAndConsequenceAudit>
Always perform this audit, including when the caller passes `skip-architect`.
For every new or changed semantic state, transition, failure, availability,
recovery condition, diagnostic, or externally observable lifecycle, record:

1. Who produces it and how long it persists.
2. Which code or application consumers can observe it.
3. Whether it is implementation-only, application-observable, or
   user-actionable.
4. For a user-actionable outcome, what the user sees or can do, including
   recovery, diagnostics, reflection/BRP, documentation, and examples where
   applicable.
5. Its destination, named from this set: a test, an example in this repository,
   a feature in a consuming application, a reified tool in a tool-graph or
   node-runtime repository, or none. Then its owner — an existing remaining Work
   Order for an in-repository destination, or `${NEXT_ITEMS_PATH}` for one this
   plan cannot implement. Name the acceptance test for every in-repository
   destination. A destination is never dropped for being blocked by a gate or
   living in another repository; that is what makes it a next item rather than a
   phase.
6. When no external surface is appropriate, `implementation-only because
   <reason>` instead of silently omitting it.

Put each disposition under `State and consequence audit` in the retrospective.
Treat an unowned required consequence as an implication for remaining phases or
a necessary next-item candidate. Treat a missing surface required by the
completed Work Order as a current-phase defect; under `/plan:delegate`, return
it to `<Synthesize/>` before shrink.
</StateAndConsequenceAudit>

## Step 3.5: Sweep process comments out of the implementation diff

Before dispatching the review, inspect the implementation diff for the phase: use the working-tree diff if the phase is uncommitted, or the phase commit diff if it has already been committed.

If the phase is uncommitted, first run `git status --short` and `git add -N` every untracked file the phase created (excluding orchestrator-owned files such as handoff docs). `git diff` does not show untracked files at all, so a new source file is invisible to this sweep and to the architect review that follows — and new files are exactly where fresh process comments accumulate. Verify by name that every file the phase claims to have created appears in the diff before continuing.

Remove or rewrite source-code comments that describe phase history, planning decisions, review process, or temporary rationale tied to the just-completed plan phase. Code comments should explain the code as it exists now, not narrate how it got there.

Remove comments that mention phase numbers, "for this phase", "per the plan", "decision from review", "temporary until Phase N", or similar process/history markers.

Keep comments that explain durable code facts: invariants, safety constraints, non-obvious API contracts, performance tradeoffs, data-format requirements, or behavior future maintainers need regardless of the plan history.

Scope this sweep narrowly:

- Only inspect files touched by the just-completed phase.
- Only edit comments. Do not change runtime behavior.
- Do not remove user-facing documentation, changelog text, or comments that are still the best place to document a durable code constraint.
- If a comment contains both process history and a durable constraint, rewrite it to keep only the durable code constraint.

If any comments were removed or rewritten, include that in the final update's `Learned and applied` row.

## Step 4: Dispatch an architect review of the remaining phases

**Skip this whole step when `$ARGUMENTS` contains `skip-architect`.** The caller
has already applied the trigger test in `/plan:delegate`'s `<RunPhaseReview/>`
and determined this phase produced nothing for an architect to find. Write
`not run — phase matched its plan` into the final update's architect row and go
straight to Step 5; the temporary retrospective's implications still get folded into the
remaining Work Orders exactly as written there. Do not second-guess the token or
re-derive the trigger test — the caller has the review results and the ledger,
this command does not.

The architect's job is an architectural review of the *remaining* phases and,
when present, `${NEXT_ITEMS_PATH}` in light of what was just implemented and
learned.

**When the caller names a scope** — a list of phases whose Work Orders reference
what actually changed — those are the architect's subject. It still reads the
others for consistency, but it verifies the named ones against real code. An
unscoped invocation covers every remaining phase as before.

**Dispatch it through the delegate launcher, not the Agent tool.** A launcher
dispatch writes a status file and streams `[wrapper]` beats with an activity
digest into the shared heartbeat log, so the orchestrating agent can watch this
review and narrate it exactly like an implementation or code review. An Agent-tool
subagent produces no readable liveness signal — its only output is a full
transcript too large to read — which leaves the user with an unexplained
multi-minute silence at the same point in every phase.

Write the prompt to `${SESSION_DIR}/architect_prompt_<phase>.md`, then:

```
bash ~/.claude/scripts/delegate/review.sh \
  "${SESSION_DIR}" "${WORKING_DIR}" \
  "${SESSION_DIR}/architect_prompt_<phase>.md" \
  architect \
  "<plan-doc filename> — phase: <phase>
architect review of the remaining phases against what phase <phase> actually shipped" \
  "reviewing the remaining phases against what just shipped" \
  <pass_index>
```

- `architect` is the subtask name; it resolves through
  `[delegate.<family>]` in `~/.claude/config/agents.conf` like every other
  delegate subtask.
- `<pass_index>` continues the phase's review numbering — one past the last code
  review pass — so `review_findings_<N>.txt` never overwrites a code review's
  findings. Findings come back in that file.
- **`SESSION_DIR`**: use the session established in Step 1.
- Apply `/plan:delegate`'s `<DispatchContract/>`; do not invent another wait or
  progress mechanism.

The prompt must include:

- The absolute path to the plan doc.
- The absolute `${NEXT_ITEMS_PATH}` and a directive to read it when it exists;
  absence means there are no approved next items to review.
- The phase that just completed (number and title).
- A directive to read the implemented code referenced by that phase (so its review is grounded in what actually exists, not what was planned).
- A directive to read
  `${SESSION_DIR}/phase_review_retrospective_<phase>.md` and not search the plan
  for retrospective or review prose.
- The complete contents of `~/.claude/docs/type_design.md`, under a `Type Design
  Contract` heading.
- The review questions:
  1. Are any remaining phases now redundant, partially redundant, or already satisfied by what was just built?
  2. Do any remaining phases need re-scoping (smaller, larger, split, merged, reordered) given the implications surfaced in the retrospective?
  3. Are there new risks, dependencies, or sequencing constraints that the plan does not yet name?
  4. Are any assumptions in the remaining phases now invalidated?
  5. Are there gaps — work the plan does not cover but that the implemented
     phase has revealed as necessary? For every state or outcome in the
     retrospective's `State and consequence audit`, confirm who can observe it,
     what a user should see or be able to do, and which remaining Work Order
     owns that surface and its acceptance test. If no external surface is
     appropriate, confirm the implementation-only reason.
  6. (Delegate-ready plans only) For each remaining phase, is its `#### Work Order` still self-contained — could a fresh codex session implement it from the named files + Delegation Context alone? Name any Work Order that now needs an added **Constraints from prior phases** fact, a corrected file/line ref, or a changed acceptance gate because of what just shipped.
  7. Do type names state their semantic role, state, lifetime, or guarantee
     without requiring the reader to inspect callers? Does any remaining phase
     introduce or retain a bare `Option<T>` in a domain-owned type or API where
     a semantic type should replace it, including conversion around an external
     API boundary?
  8. If `${NEXT_ITEMS_PATH}` exists, did this phase or the revised remaining plan
     make any item inaccurate, redundant, already satisfied, or aimed at the
     wrong target? Quote the current item and propose its exact replacement or
     removal with concrete evidence. Do not propose optional polish.
- **The narration instruction**, verbatim: *"Narrate as you go: before each new
  activity, output one short present-tense line of plain text naming it. These
  lines stream to a liveness monitor."* This is what makes the dispatch legible
  in the wrapper heartbeat digest. Do **not** give the architect the heartbeat file path —
  it runs read-only and cannot write; its narration reaches the heartbeat log
  through the `[wrapper]` digest instead. Same rule as a code review prompt.
- Output format: a numbered list of findings. Each finding has a one-line title,
  a body of one to three sentences, and a `Severity:` tag — `minor` (safe to edit
  straight into the plan), or `significant` (changes scope, ordering, or
  architectural intent and needs user approval before editing). A Q8 finding
  also has `Destination: next-items`, `Action: add|amend|remove`, `Current:`
  quoted verbatim for `amend`/`remove`, `Target:`, and `Proposed:` with the exact
  new item, replacement text, or `remove`.

The architect does **not** edit the plan. It returns findings only.

## Step 5: Fold findings back into the plan

<NextItemAmendments>
Before normal finding routing, remove every `Destination: next-items` finding
from the plan-finding set. Deduplicate by current item and observable outcome,
reject unsupported or optional changes, then write validated proposals to
`${SESSION_DIR}/next_item_amendments_<phase>.md` with `Action`, `Current`,
`Target`, `Proposed`, `Why`, source phase, and **`Class`**.

`Class` is `apply` or `gate`, and it decides whether the user is asked. This file
is a backlog: writing an item into it commits nobody to building it, and the
decision that matters happens later, when an item is scheduled into a phase. What
earns a gate is destroying or rewriting a record the user may have already read —
not creating one.

A proposal is **`apply`** when it is purely additive or purely factual: any `add`,
and any `amend` that only makes an existing item agree with code that has already
shipped while leaving it asking for exactly the work it asked for before — a
drifted file or line reference, a stated dependency this phase satisfied, a named
consequence this phase created, a claim this phase falsified.

A proposal is **`gate`** when it removes or rewrites what is already recorded: any
`remove`, and any `amend` that changes what the item asks for, what would satisfy
it, or what it targets. When the split is genuinely unclear, `gate`.

**A defect in what this phase just shipped is never an `add`.** It is a
current-phase defect: under `/plan:delegate` return it to <Synthesize/>, and
standalone fix it before Step 6. A Work Order's **Files** list is the scope the
plan predicted, not a limit on what this phase may repair.

Under `/plan:delegate`, stop there; <ConsiderNextItems/> writes the `apply` ones
and owns the gate for the rest, plus auto-window batching. Invoked standalone,
write every `apply` proposal to `${NEXT_ITEMS_PATH}` now and report it as one line
naming the count and the file; present only the `gate` proposals through the
approve/revise/reject gate before Step 6; apply approved ones; then delete the
artifact once every proposal is resolved.

Do not route an `apply` proposal to the user under any framing — not as a
question, not as a list awaiting acknowledgement, not as a "confirm before I
apply". A correction with verified evidence and no alternative answer is not a
decision, and presenting it as one spends the user's turn to buy nothing.
</NextItemAmendments>

Execute <NextItemAmendments/> before `<MinorFindings/>` or
`<SignificantFindings/>`.

<MaintainWorkOrders>
**Delegate-ready plans only** (skip for plans not in the format-doc structure).
Before processing findings, keep the remaining Work Orders dispatch-ready:

**Write boundary:** only remaining `todo` Work Orders may change. The current
phase is replaced later by shrink; every earlier `done` phase must remain
byte-identical.

1. **Propagate forward.** Apply the **Propagate-Forward** rule from the format doc
   (`~/.claude/docs/delegate_plan_format.md` → "Forward-propagation") for the facts
   the just-shipped phase produced. This is the single most important maintenance
   step: it is what lets the next `/plan:delegate` assemble without research.
2. **Apply each Q6 finding** by editing the named Work Order in place — add the
   missing constraint, fix the drifted file/line ref, adjust the **Spec** or
   **Acceptance gate**. Do not record these as prose-only notes; the Work Order
   text itself must change so it stays self-contained.
3. **Self-containment check.** After edits, each remaining Work Order must still be
   implementable from its named files + Delegation Context alone. If a finding
   widened scope, update **Files** and **Spec** to match.
4. **Own every consequence.** For each application-observable or
   user-actionable audit disposition, name the remaining Work Order that owns
   its surface and acceptance test. If none does, route the required work as a
   plan gap or next-item candidate; never leave it only in retrospective prose.

Mechanical Work Order edits (added constraints, corrected refs, gate tweaks) need
no user gate — they go straight in. A finding that changes a remaining phase's
*intent, scope, or ordering* is a significant finding: route it through
`<SignificantFindings/>` below, and once resolved, write the outcome into the
affected Work Order, not a prose review note.
</MaintainWorkOrders>

<MinorFindings>
Edit each minor finding straight into the plan — inline amendment to the affected remaining phase or a short note under that phase. No user gate. For a delegate-ready plan, "inline amendment" means editing the affected phase's Work Order per `<MaintainWorkOrders/>`.
</MinorFindings>

<SignificantFindings>
**`<DecisionEconomy/>` binds this command**, by the same import `/plan:delegate`
uses. Read it before deciding anything reaches the user:

@~/.claude/docs/decision_criteria.md

**Forbidden tool: `AskUserQuestion`.** Surveys collapse the decision to a one-line label and strip the concrete recommendation. If you reach for `AskUserQuestion`, stop and route through `<FilterFindingsForUserReview/>` first.

Execute `<FilterFindingsForUserReview/>` before presenting anything to the user. Subagent `Severity: significant` means "needs filtering," not automatically "ask the user."

**Auto mode:** do not execute `<PresentInlineSingle/>` or `<DispatchAdhocReview/>`. For each unresolved user decision after filtering, write a `**Pending decision:**` block containing the filled `<DecisionPresentationTemplate/>` into the Work Order of the earliest affected remaining phase — the `/plan:delegate` loop stops for it at that phase's pre-dispatch check. List each deferral in the final update's `User decisions` row as `deferred to phase N: <one-line title>`. Then skip the rest of this block.

After filtering, count unresolved user decisions, not raw subagent findings:

- 0 unresolved user decisions → skip this block.
- 1 unresolved user decision → execute `<PresentInlineSingle/>`.
- 2+ unresolved user decisions → execute `<DispatchAdhocReview/>`.

There is no fixed maximum number of user decisions. If filtering leaves a large list, think harder about grouping, mechanical edits, and already-implied work before invoking `/adhoc_review`; if a decision is truly distinct, present it.
</SignificantFindings>

<FilterFindingsForUserReview>
Convert raw significant findings into real user decisions. `<UserFacingText/>`'s
four tests — is it real, does it have at least two buildable answers, is it the
user's, would the finished thing differ — decide what survives; these steps are
how they apply here:

1. Apply mechanical plan-doc findings directly.
2. Merge duplicate findings that point to the same actual decision.
3. Drop findings that only restate work already implied by the current phase.
4. Convert abstract findings into the concrete implementation problem:
   - What code, file, type, module, phase, or behavior is missing or wrong?
   - Which phase should create or change it?
   - What exact plan text should be added or replaced?
5. If a finding says an abstraction, contract, API, or boundary is incomplete, determine whether:
   - the completed phase was supposed to create it,
   - the next phase is supposed to create it,
   - or the plan is missing a task or phase that should create it.
   Present that answer directly.
6. **Resequence rather than ask.** When the plan already defines the behavior and
   only the shape of the work is wrong, split, merge, reorder, or renumber the
   phases yourself, preserve scope, API, invariants, tests, and ownership, update
   the affected Work Orders, and continue. State the change in one line. This is
   `<DecisionRouting/>`'s first rule and it is not discretionary: a phase that grew
   too large, a seam in the wrong place, or two phases that should be one are
   decisions about how work is packaged, not about what gets built, and they cost
   a one-line revert. Never spend a user turn on one.
7. Make a recommendation. Do not ask the user to reason from labels.

Only decisions that change **what gets built** survive this filter: product
behavior, architecture direction, an externally observable contract, or whether a
piece of scope exists at all. **How committed work is packaged never survives it**
— phase count, phase boundaries, ordering, numbering, and which phase owns a task
are the orchestrator's to decide. If the answer to "what will the finished
deliverable do?" is the same under both options, it is not the user's decision.
</FilterFindingsForUserReview>

<DecisionPresentationTemplate>
Every user-facing decision must use this structure:

```markdown
**Decision N: <concrete thing to decide>**

Actual problem:
<one or two sentences about the implementation issue, naming files/types/phases>

What exists now:
- <concrete current code/doc state>

What should change:
- <recommended plan/code direction>

Recommendation:
<direct recommendation, with the exact phase/doc placement>

Approve this direction, or modify it?
```

Do not present a finding as "the architect flagged X". The user should not have to infer the actual task from review vocabulary.
</DecisionPresentationTemplate>

<RequiredSubSections>
When a decision needs source detail, include it inside `<DecisionPresentationTemplate/>` using these facts:

1. **What the plan currently says** — quote the exact line(s) being changed.
2. **What just shipped** — concrete files / types / line numbers; the gap that triggered the finding.
3. **Why it matters** — what breaks, what regresses, or what test fires if left as-is.
4. **The proposed plan change** — exact replacement or insertion text.
</RequiredSubSections>

<PresentInlineSingle>
Write the decision using `<DecisionPresentationTemplate/>`. Ask once for approve / reject / redirect. Apply on approve. Drop or apply the user's redirect on rejection. If the user answers with confusion instead of a decision, execute `<ExplainOnDemand/>` and re-ask — a decision the user cannot restate is not resolved.
</PresentInlineSingle>

<UserFacingText>
**Applies to every turn this command shows the user** — inline decisions,
`/adhoc_review` items, the Step 6 final update.

**Read `~/.claude/docs/user_facing_explanation.md` and follow it.** It owns the
principle the presentation rules here derive from — you do the reconstruction,
not the user — plus the build order, naming, banned vocabulary, the
comprehension gate, which decisions are worth the user's attention, and the
choice-line format. `/plan:delegate` and `/adhoc_review` share the same file, so
the three cannot drift.
</UserFacingText>

<ExplainOnDemand>
**Trigger:** the user says they do not understand, asks what something means,
asks for a reframe, or answers a decision with confusion instead of an answer.
This fires wherever it happens — an inline decision, an `/adhoc_review` item,
the Step 6 final update — and preserves whatever was pending. Explaining never
counts as approval.

**Read `~/.claude/docs/explain_on_demand.md` and follow it.** It owns the
method: rebuild from the bottom, stay technical, name real signatures read from
real source, and put a short code example under every mechanism — problem code
before fix code. `/plan:delegate` shares the same file, so the two commands
cannot drift.

This is the one place terseness is wrong. Do not compress and do not re-emit the
same wording softened. Afterwards, restate the pending question and wait.
</ExplainOnDemand>

<DispatchAdhocReview>
Invoke `/adhoc_review` with the filtered user decisions. Each item must already use `<DecisionPresentationTemplate/>` so the user can decide one at a time without translating abstract review language. Apply each user decision into the plan as the walkthrough completes that item.
</DispatchAdhocReview>

Write `${SESSION_DIR}/phase_review_outcomes_<phase>.md` instead of appending any
review block to the plan:

```text
Phase <id>: <title>

Shipped:
- <concrete behavior, types, and modules now present>
Files:
- <path — current role>
Gotchas:
- <durable implementation constraint, if any>
Consequences:
- <state/outcome — classification, audience, surface or implementation-only reason, and owning phase/test>
Ruled out:
- <rejected proposal in one clause, if any>
Forward propagated:
- <affected todo phase and exact Work Order change, if any>
```

This is the closeout input for `/plan:shrink`; do not paste raw findings into it.
The plan retains only the resulting `As-built` block and edits already applied
to remaining Work Orders.

## Step 6: Final user update

This is the command's final step and final output. Run it only after all plan edits are complete, including any inline decision or optional `/adhoc_review` decisions that were applied back into the plan.

Produce a succinct markdown table:

```markdown
| Area | Update |
| --- | --- |
| Implemented | <one sentence naming the completed phase and concrete implementation scope> |
| Learned and applied | <one sentence naming what the retrospective/review changed in the plan automatically; use "None" if nothing was applied automatically> |
| User decisions | <one sentence summarizing inline or `/adhoc_review` decisions and their outcomes; use "None" if no user decisions were needed> |
| Recommended next step | <if a next phase exists: one sentence summarizing what that phase will build or accomplish, then one sentence with the direct recommendation; if no phases remain: just the direct recommendation> |
```

Style rules for the final update:

- `<UserFacingText/>` applies to every row. Name files, types, phases, and plan
  sections **only when** the name itself is informative; otherwise say what the
  thing does.
- Terse. One short sentence per table row.
- Do not echo the whole retrospective; summarize only what was learned and actually applied back to the document.
- Do not include passed-check filler. If every remaining phase came through clean, say that in `Learned and applied`.
- Include rejected or deferred `/adhoc_review` decisions in `User decisions` so future passes do not relitigate them.

## Rules

- Do not modify implementation code in this command except for Step 3.5's narrow source-code comment cleanup. That cleanup may only remove or rewrite process/history comments from the just-completed phase diff; behavioral code changes belong to the next phase or to a follow-up.
- Do not commit any changes.
- Do not relitigate the just-completed phase's implementation. The temporary retrospective records what was learned; the review is about what comes next.
- Raw significant findings must be filtered before user review. Only unresolved user decisions go through the user; mechanical changes and already-implied work go straight into the plan.
- User decisions never use `AskUserQuestion`. Single decision → inline decision template; two or more → `/adhoc_review`. See `<SignificantFindings/>`, `<FilterFindingsForUserReview/>`, and `<DecisionPresentationTemplate/>` in Step 5.
- In auto mode this command asks the user nothing: unresolved decisions become `**Pending decision:**` blocks in the affected Work Orders, surfaced later by the `/plan:delegate` pre-dispatch check.
- Next-item amendments never use plan finding routing and never edit the next
  file without approval; `/plan:delegate` batches them at its normal boundary.
- Never write retrospective, finding, reviewer, pass, or approval prose into the
  plan. Review text exists only under `${SESSION_DIR}` until the phase checkpoint.
- Never edit an earlier `done` phase. A finding about past work becomes an
  `As-built` fact for the current phase, a forward Work Order change, or a new
  pending decision.
- Any signal that the user does not understand triggers `<ExplainOnDemand/>` and preserves whatever was pending. Terseness is the default everywhere else; here it is the defect.
