---
description: After implementing a phase of a multi-phase plan, append a retrospective, run a subagent review of remaining phases, fold the findings back into the plan, and summarize for the user.
---

Use this after the agent has just finished implementing a phase of a multi-phase plan that is already in conversation context. The command updates the plan with what was learned, then asks an architect subagent to re-evaluate the remaining phases against that learning, then folds the findings back into the plan and reports a plain-language summary.

## Step 1: Locate the plan doc

The plan doc should already be in conversation context — it is the doc the just-completed phase came from.

- If exactly one plan doc is in scope, use it.
- If `$ARGUMENTS` names a path, use that path (overrides inference).
- If no plan doc is in scope, **ask the user** for the path before proceeding. Do not guess. This case should be rare.

State the path you picked in one line: `Reviewing <relative/path/to/plan.md>.`

## Step 2: Identify the phase that just completed

Infer from conversation: the most recently implemented phase is the one being reviewed. State which phase in one line: `Phase under review: <phase number / title from the plan>.`

If the conversation does not make the phase obvious, ask the user one clarifying question and wait.

## Step 3: Append a retrospective and mark the phase complete

Edit the plan doc to:

1. Mark the phase complete in whatever convention the plan already uses (checkbox, status line, heading suffix). Match the existing style — do not introduce a new one.
2. Append a **Retrospective** subsection inside that phase (or directly after it if the phase has no body container). Use this template:

   ```markdown
   ### Retrospective

   **What worked:** <one or two terse bullets>
   **What deviated from the plan:** <bullets — scope changes, approach changes, anything the plan did not predict>
   **Surprises:** <bullets — things learned during implementation that the plan author did not know>
   **Implications for remaining phases:** <bullets — concrete effects on later phases; this is the bridge into Step 4>
   ```

   Drop any subsection that has nothing to say. Keep bullets short and concrete — name files, types, decisions, not abstractions.

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
- Output format: a numbered list of findings. Each finding has a one-line title, a body of one to three sentences, and a `Severity:` tag — `minor` (safe to edit straight into the plan), or `significant` (changes scope, ordering, or architectural intent and needs user approval before editing).

The subagent does **not** edit the plan. It returns findings only.

## Step 5: Fold findings back into the plan

<MinorFindings>
Edit each minor finding straight into the plan — inline amendment to the affected remaining phase or a short note under that phase. No user gate.
</MinorFindings>

<SignificantFindings>
**Forbidden tool: `AskUserQuestion`.** Surveys collapse the decision to a one-line label and strip the concrete recommendation. If you reach for `AskUserQuestion`, stop and route through `<FilterFindingsForUserReview/>` first.

Execute `<FilterFindingsForUserReview/>` before presenting anything to the user. Subagent `Severity: significant` means "needs filtering," not automatically "ask the user."

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

## Step 6: Summarize to the user

Produce a short plain-language summary in this format:

```
<one-line lead: phase X reviewed; M findings, K applied, S sent for approval>

1. <finding title> → <what happened: applied / approved & applied / rejected / pending approval>
2. ...
```

Style rules for the summary:

- Plain language. Name files, types, phases — not abstractions.
- Terse. One short line per finding.
- Do not echo the retrospective back; the user wrote it through you and already knows it.
- Do not include passed-check filler ("phase 3 is fine"). Silence is the signal.
- If every remaining phase came through clean, say so in one line.

## Rules

- Do not modify implementation code in this command — this is plan-doc maintenance only. Code changes belong to the next phase or to a follow-up.
- Do not commit any changes.
- Do not relitigate the just-completed phase's implementation. The retrospective records what was learned; the review is about what comes next.
- Raw significant findings must be filtered before user review. Only unresolved user decisions go through the user; mechanical changes and already-implied work go straight into the plan.
- User decisions never use `AskUserQuestion`. Single decision → inline decision template; two or more → `/adhoc_review`. See `<SignificantFindings/>`, `<FilterFindingsForUserReview/>`, and `<DecisionPresentationTemplate/>` in Step 5.
- If the subagent returns nothing actionable, still append the **Phase N Review** block with a single line stating the remaining phases were reviewed and need no changes.
