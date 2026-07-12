---
description: After implementing a phase of a multi-phase plan, append a retrospective, run a subagent review of remaining phases, fold the findings back into the plan, and summarize for the user.
---

Use this after the agent has just finished implementing a phase of a multi-phase plan that is already in conversation context. The command updates the plan with what was learned, then asks an architect subagent to re-evaluate the remaining phases against that learning, then folds the findings back into the plan and reports a jargon-free summary.

**Delegate-ready plans.** If the plan follows `~/.claude/docs/delegate_plan_format.md` (a `## Delegation Context` section + per-phase `#### Work Order`), this command must keep every remaining phase **dispatch-ready**: learnings are folded *into* the remaining Work Orders (Spec, Files, Acceptance gate, and especially **Constraints from prior phases**), not merely appended as review notes. The test after this command runs: `/plan:delegate <plan> phase <next>` can assemble its prompt with zero codebase research. See `<MaintainWorkOrders/>` in Step 5.

**Auto mode.** If `$ARGUMENTS` contains the token `auto` (passed by the `/plan:delegate` loop), this command asks the user nothing: significant findings that survive filtering are deferred into the affected phase's Work Order as `**Pending decision:**` blocks (format: `~/.claude/docs/delegate_plan_format.md`) instead of being presented. Invocations without `auto` behave exactly as written below.

## Step 1: Locate the plan doc

The plan doc should already be in conversation context — it is the doc the just-completed phase came from.

- Strip the `auto` token from `$ARGUMENTS` first — it is a mode switch, not a path.
- If exactly one plan doc is in scope, use it.
- If `$ARGUMENTS` names a path, use that path (overrides inference).
- If no plan doc is in scope, **ask the user** for the path before proceeding. Do not guess. This case should be rare.

State the path you picked in one line: `Reviewing <relative/path/to/plan.md>.`

## Step 2: Identify the phase that just completed

Infer from conversation: the most recently implemented phase is the one being reviewed. State which phase in one line: `Phase under review: <phase number / title from the plan>.`

If the conversation does not make the phase obvious, ask the user one clarifying question and wait.

## Step 3: Append a retrospective and mark the phase complete

Edit the plan doc to:

1. Mark the phase complete in whatever convention the plan already uses (checkbox, status line, heading suffix). Match the existing style — do not introduce a new one. For a delegate-ready plan, set the phase `status: done (<commit>)` and keep its `#### Work Order` verbatim as the archive record. In auto mode the commit does not exist yet — write `status: done`; the `/plan:delegate` checkpoint commit adds the hash afterwards.
2. Append a **Retrospective** subsection inside that phase (or directly after it if the phase has no body container). Use this template:

   ```markdown
   ### Retrospective

   **What worked:** <one or two terse bullets>
   **What deviated from the plan:** <bullets — scope changes, approach changes, anything the plan did not predict>
   **Surprises:** <bullets — things learned during implementation that the plan author did not know>
   **Implications for remaining phases:** <bullets — concrete effects on later phases; this is the bridge into Step 4>
   ```

   Drop any subsection that has nothing to say. Keep bullets short and concrete — name files, types, decisions, not abstractions.

## Step 3.5: Sweep process comments out of the implementation diff

Before dispatching the review, inspect the implementation diff for the phase: use the working-tree diff if the phase is uncommitted, or the phase commit diff if it has already been committed.

Remove or rewrite source-code comments that describe phase history, planning decisions, review process, or temporary rationale tied to the just-completed plan phase. Code comments should explain the code as it exists now, not narrate how it got there.

Remove comments that mention phase numbers, "for this phase", "per the plan", "decision from review", "temporary until Phase N", or similar process/history markers.

Keep comments that explain durable code facts: invariants, safety constraints, non-obvious API contracts, performance tradeoffs, data-format requirements, or behavior future maintainers need regardless of the plan history.

Scope this sweep narrowly:

- Only inspect files touched by the just-completed phase.
- Only edit comments. Do not change runtime behavior.
- Do not remove plan-doc retrospective notes, user-facing documentation, changelog text, or comments that are still the best place to document a durable code constraint.
- If a comment contains both process history and a durable constraint, rewrite it to keep only the durable code constraint.

If any comments were removed or rewritten, include that in the final update's `Learned and applied` row.

## Step 4: Dispatch a subagent review of the remaining phases

Spawn a `Plan` subagent with a self-contained prompt. The subagent's job is an architectural review of the *remaining* phases in light of what was just implemented and learned.

The prompt to the subagent must include:

- The absolute path to the plan doc.
- The phase that just completed (number and title).
- A directive to read the implemented code referenced by that phase (so its review is grounded in what actually exists, not what was planned).
- A directive to read the **Retrospective** that was just appended.
- The review questions:
  1. Are any remaining phases now redundant, partially redundant, or already satisfied by what was just built?
  2. Do any remaining phases need re-scoping (smaller, larger, split, merged, reordered) given the implications surfaced in the retrospective?
  3. Are there new risks, dependencies, or sequencing constraints that the plan does not yet name?
  4. Are any assumptions in the remaining phases now invalidated?
  5. Are there gaps — work the plan does not cover but that the implemented phase has revealed as necessary?
  6. (Delegate-ready plans only) For each remaining phase, is its `#### Work Order` still self-contained — could a fresh codex session implement it from the named files + Delegation Context alone? Name any Work Order that now needs an added **Constraints from prior phases** fact, a corrected file/line ref, or a changed acceptance gate because of what just shipped.
- Output format: a numbered list of findings. Each finding has a one-line title, a body of one to three sentences, and a `Severity:` tag — `minor` (safe to edit straight into the plan), or `significant` (changes scope, ordering, or architectural intent and needs user approval before editing).

The subagent does **not** edit the plan. It returns findings only.

## Step 5: Fold findings back into the plan

<MaintainWorkOrders>
**Delegate-ready plans only** (skip for plans not in the format-doc structure).
Before processing findings, keep the remaining Work Orders dispatch-ready:

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

Mechanical Work Order edits (added constraints, corrected refs, gate tweaks) need
no user gate — they go straight in. A finding that changes a remaining phase's
*intent, scope, or ordering* is a significant finding: route it through
`<SignificantFindings/>` below, and once resolved, write the outcome into the
affected Work Order, not just the Phase N Review block.
</MaintainWorkOrders>

<MinorFindings>
Edit each minor finding straight into the plan — inline amendment to the affected remaining phase or a short note under that phase. No user gate. For a delegate-ready plan, "inline amendment" means editing the affected phase's Work Order per `<MaintainWorkOrders/>`.
</MinorFindings>

<SignificantFindings>
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
Convert raw significant findings into real user decisions:

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
6. Make a recommendation. Do not ask the user to reason from labels.

Only decisions that change product behavior, architecture direction, phase ordering, or implementation scope survive this filter.
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
Write the decision using `<DecisionPresentationTemplate/>`. Ask once for approve / reject / redirect. Apply on approve. Drop or apply the user's redirect on rejection.
</PresentInlineSingle>

<DispatchAdhocReview>
Invoke `/adhoc_review` with the filtered user decisions. Each item must already use `<DecisionPresentationTemplate/>` so the user can decide one at a time without translating abstract review language. Apply each user decision into the plan as the walkthrough completes that item.
</DispatchAdhocReview>

Then append a **Phase N Review** block under the just-completed phase summarizing what the review changed:

```markdown
### Phase N Review

- <one bullet per finding that resulted in a plan edit, naming the affected remaining phase and the change>
- <bullets for findings the user rejected, marked as such, so future passes do not relitigate>
```

Do not paste the raw subagent output into the plan — only the resolved outcomes.

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

- Write for someone who has not read the plan or the diff. Name files, types,
  phases, and plan sections **only when** the name itself is informative;
  otherwise say what the thing does. Never present a reviewer's label, decision
  code, test/guard name, or tooling term (`headless`, `bind group`) as if the
  user already knows it — translate it to its behavior. If you cannot state a row
  in behavior terms, you do not understand it well enough to summarize it.
- Never use the word "plain" or any variant (`plain language`, `plain terms`,
  `in plain English`) anywhere in the output. Write that way without announcing
  it. Absolute.
- Terse. One short sentence per table row.
- Do not echo the whole retrospective; summarize only what was learned and actually applied back to the document.
- Do not include passed-check filler. If every remaining phase came through clean, say that in `Learned and applied`.
- Include rejected or deferred `/adhoc_review` decisions in `User decisions` so future passes do not relitigate them.

## Rules

- Do not modify implementation code in this command except for Step 3.5's narrow source-code comment cleanup. That cleanup may only remove or rewrite process/history comments from the just-completed phase diff; behavioral code changes belong to the next phase or to a follow-up.
- Do not commit any changes.
- Do not relitigate the just-completed phase's implementation. The retrospective records what was learned; the review is about what comes next.
- Raw significant findings must be filtered before user review. Only unresolved user decisions go through the user; mechanical changes and already-implied work go straight into the plan.
- User decisions never use `AskUserQuestion`. Single decision → inline decision template; two or more → `/adhoc_review`. See `<SignificantFindings/>`, `<FilterFindingsForUserReview/>`, and `<DecisionPresentationTemplate/>` in Step 5.
- In auto mode this command asks the user nothing: unresolved decisions become `**Pending decision:**` blocks in the affected Work Orders, surfaced later by the `/plan:delegate` pre-dispatch check.
- If the subagent returns nothing actionable, still append the **Phase N Review** block with a single line stating the remaining phases were reviewed and need no changes.
