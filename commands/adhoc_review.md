---
description: Walk through decisions one at a time using concise necessary context, one atomic question, a recommendation, relevant pending decisions, and optional working-doc synchronization.
---

Use this when the user has just received a long list — recommendations, findings, options, todos — and wants to review them deliberately one by one instead of responding to the whole wall of text.

## Step 1: Identify the items

Find the list to review. Look in this order:

1. The user's invocation contains the list (pasted in, or pointing at a section).
2. The most recent assistant message had an enumerated list — bullets, numbered items, table rows.
3. Neither — ask the user where the list is.

If more than one list could be intended, ask which list or section; do not
combine them. Otherwise, determine the count but do not reply yet. Carry the
count into the Step 2 question without listing the items.

## Step 2: Set up where decisions get recorded

Decide whether there is a working doc in this conversation — a file path the user has been editing or named this session.

Then ask **one** question, picking the form that matches the situation:

- **A doc is already in scope:** name it.
  > `N items. Record decisions in <relative/path/to/doc.md> (recommended)? (yes / different path / none)`
- **No doc is in scope:** say so.
  > `N items. No working doc in this conversation. Record decisions in a doc? (suggest one (recommended) / path to use / none)`

Wait for the answer before creating a doc or continuing to Step 3.

Rules:
- Never use the placeholder phrase "existing path" — either name the doc or say there isn't one.
- If the user gives a path that doesn't exist, create it with a one-line header (today's date + where the items came from).
- If the user picks `none`, skip the doc-writing step in Step 4 and just summarize at the end.

## Step 3: Build the todo list

If a todo tool is available (e.g. `TaskCreate`), add one task per item, in order, each with a short label and status `pending`. If not, keep an inline numbered checklist and refer to items by number.

## Step 4: Walk the list

For each item, in order:

1. Mark the task `in_progress`.
2. Check that the item contains **one decision**. If it contains independent questions, split them into ordered subitems, preserve the original ID as their prefix, update the task list, and present only the first.
3. Present the item with the decision frame below. Keep every section short; omit a section only when it truly has no content.
4. **Present the choices as an inline text line, and always mark one as recommended.** Pick a choice set that fits the item — `keep / drop / modify`, `approve / reject / redirect`, etc. — append `(recommended)` to exactly one option with a one-line reason, and always include `elaborate` as a choice. The answer request is a single line of plain message text, e.g.:
   > `keep (recommended — survived review unchanged) / modify / drop / elaborate`
   NEVER present an item through a survey/questionnaire mechanism (AskUserQuestion or any multiple-choice UI). Summary and choices are ordinary message text; the user answers by typing. Terse is good, cryptic is not.
5. **Wait for the user's response.** Do not move on until they reply.
   - If the user picks `elaborate` (also: "more", "detail", "expand", "why"): add one new rationale, constraint, or example relevant to the current item. Do not broaden scope or dump the raw item. Then re-present the same choices and wait again.
   - If the user asks a clarifying question, answer it without recording a decision or advancing. Re-present the choices only when useful.
   - If the user proposes a modification, restate only the revised decision, update the recommendation or example as needed, and wait for explicit acknowledgment before recording it.
6. When they clearly acknowledge a terminal choice (including terse approvals such as `agreed`, `approved`, `okay`, or `continue` when unambiguous), record the decision to the working doc if one is in scope, mark the task `completed`, and move to the next item.

Do not ask "continue or add more?" between items — assume continue unless the user volunteers something.

### Decision frame

Use this order by default:

1. **Necessary context** — two to six short statements containing only what the user must know to evaluate this item.
   - Define each relevant file, type, API, rule, or prior decision in one plain sentence.
   - Use exact domain and type-system names. Distinguish `existing`, `confirmed but not implemented`, and `proposed` concepts.
   - Mark every undecided name immediately as `(name TBD)`.
   - Do not rely on the user remembering earlier turns, but do not reset the entire review.
2. **Question** — state the single decision being made now in one sentence.
3. **Recommendation** — give the recommended outcome first, then the smallest useful rationale. Usually two to four bullets are enough.
4. **New-behavior example** — for API or workflow decisions, show one small example of the proposed behavior. Do not volunteer a before-example.
5. **Still pending** — name only nearby decisions that could otherwise look silently decided. Say where they will be handled when known. Do not dump the full backlog.
6. **Choices** — one plain-text line with exactly one recommendation and `elaborate`.

Example shape:

```text
### A3 — <decision title>

Necessary context:
- `ExistingType` is ...
- `PlannedType` is confirmed but not implemented; it ...

The question is whether ...

I recommend ... because ...

<small new-behavior example, when useful>

Still pending:
- ... remains for A4.

approve (recommended — reason) / alternative / elaborate
```

This is a scaffold, not a demand for headings when a very small item reads more clearly in a few sentences.

## Step 5: Wrap up

When every item is done:

- If a working doc was used: one-line summary — `Wrote decisions for N completed items to <path>`.
- Otherwise: summarize inline, one line per item — `1. <label> → <decision>`.
- Ensure every task has a terminal status.

## Rules

- **NEVER run the review as a survey.** No AskUserQuestion, no option chips, no questionnaire UI — for the items themselves or for any step of this workflow. Every item is a succinct, clear plain-text summary followed by the inline choice line defined in Step 4. Clear beats simple: keep the technical content, drop the widget.
- One item at a time. Never present two items in the same turn.
- Default presentation uses the decision frame: enough local context to decide without remembering prior turns, but not the full review history. `elaborate` adds one notch of detail, never a wholesale dump of the raw item.
- Never introduce an unexplained name or substitute vague shorthand for an agreed domain/type name. If a name is undecided, label it `(name TBD)` instead of making it sound settled.
- Keep the recommendation scoped to the active question. Put adjacent unresolved consequences in **Still pending** rather than deciding them implicitly.
- For technical changes, show the proposed/new behavior only unless the user asks for the before state.
- Whenever you present discrete choices — here or in Step 2 — always mark exactly one as recommended with a one-line reason. Open prompts ("any thoughts?") are exempt.
- Don't echo the user's feedback back to them unless asked — just record it.
- If the user says skip, use a supported skipped/cancelled terminal status; if none exists, mark it completed with the skip noted. Then move on without arguing.
- If they want to revisit an earlier item, jump back. Don't insist on linear order.
- Keep prompts terse but readable. Every extra word costs them — but so does cryptic shorthand.
- If the user asks for plain-English explanation of an item, give it without abandoning the one-at-a-time rhythm.
- If the review maintains a decision ledger, update it only after acknowledgment and leave undecided decision cells blank. Show it after each acknowledgment only when the user requested a running ledger; otherwise show it at wrap-up.
- When the user adds a later review item, record it without forcing an immediate decision; place it after any prerequisites they named.
