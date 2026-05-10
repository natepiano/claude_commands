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
**Forbidden tool: `AskUserQuestion`.** Surveys collapse the finding to a one-line label and strip the four required sub-sections. If you reach for `AskUserQuestion` for a significant finding, you are about to violate this rule — stop and route through one of the two paths below instead.

**Count first, then route:**

- 0 significant findings → skip this block.
- 1 significant finding → execute `<PresentInlineSingle/>`.
- 2+ significant findings → execute `<DispatchAdhocReview/>`.

**Every significant finding presented (inline or via `/adhoc_review`) must include all four sub-sections.** See `<RequiredSubSections/>`.

Do not assume the user has read the subagent output. Do not write "the architect flagged X" without first explaining X. Do not ask the user to choose between abstract options ("A vs B") without showing the concrete trade-off in code or plan terms.
</SignificantFindings>

<RequiredSubSections>
1. **What the plan currently says** — quote the exact line(s) being changed.
2. **What just shipped** — concrete files / types / line numbers; the gap that triggered the finding.
3. **Why it matters** — what breaks, what regresses, what re-blesses, what test fires if left as-is.
4. **The proposed plan change** — exact replacement or insertion text.
</RequiredSubSections>

<PresentInlineSingle>
Write the four-sub-section prose to the user inline. Ask once for approve / reject / redirect. Apply on approve. Drop or apply the user's redirect on rejection.
</PresentInlineSingle>

<DispatchAdhocReview>
Invoke `/adhoc_review` with the list of findings. Each finding's body must include the four sub-sections from `<RequiredSubSections/>` so the user can decide one at a time without flipping back to source. Apply each user decision (approve / reject / redirect) into the plan as the walkthrough completes that item.
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
- Significant findings always go through the user before being written into the plan. Minor findings do not.
- Significant findings never use `AskUserQuestion`. Single finding → inline four-sub-section prose; two or more → `/adhoc_review`. See `<SignificantFindings/>` and `<RequiredSubSections/>` in Step 5.
- If the subagent returns nothing actionable, still append the **Phase N Review** block with a single line stating the remaining phases were reviewed and need no changes.
