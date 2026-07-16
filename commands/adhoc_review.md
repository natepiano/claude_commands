---
description: Walk through decisions one at a time using behavior-first, decision-ready context, one atomic question, a recommendation, relevant pending decisions, and optional working-doc synchronization.
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
3. **Build a decision-ready explanation before formatting the item.** Reviewer knowledge, source-document terminology, and names introduced by another agent do not count as user knowledge.
   - Start with the situation the user is trying to handle: what happens, who or what responds, and why a choice is needed.
   - Explain every new concept by its behavior before giving its code/API name. If the name is unnecessary for the decision, omit it.
   - For every option or enum variant, state what the application or user would observe and the important tradeoff. A list of names is not an explanation.
   - Use one concrete scenario when timing, lifecycle, ownership, or state transitions create the decision.
   - Distinguish existing behavior from proposed behavior, but do not make the user reconstruct the causal story from implementation facts.
4. Apply the **comprehension gate** before presenting the item. The explanation is not ready unless a reader who has not read the source can answer all three:
   - Why does this decision exist?
   - What would each choice make the system do?
   - What user goal or failure mode makes the recommendation preferable?

   If any answer depends on an unexplained type, variant, acronym, file, or prior review finding, rewrite the explanation from the user's situation. Do not rely on `elaborate` to supply context required for the initial decision.
5. Present the item with the decision frame below. Brevity is applied only after the comprehension gate passes; there is no sentence or bullet budget when more context is required to make the decision intelligible.
6. **Present the choices as an inline text line, and always mark one as recommended.** Write choices in behavioral language first, with code names parenthetically only when useful. Pick a choice set that fits the item — `keep / drop / modify`, `approve / reject / redirect`, etc. — append `(recommended)` to exactly one option with a one-line reason, and always include `elaborate` as a choice. The answer request is a single line of plain message text, e.g.:
   > `keep (recommended — survived review unchanged) / modify / drop / elaborate`
   NEVER present an item through a survey/questionnaire mechanism (AskUserQuestion or any multiple-choice UI). Summary and choices are ordinary message text; the user answers by typing. Terse is good, cryptic is not.
7. **Wait for the user's response.** Do not move on until they reply.
   - If the user picks `elaborate` (also: "more", "detail", "expand", "why"): add one new rationale, constraint, or example relevant to the current item. Do not broaden scope or dump the raw item. Then re-present the same choices and wait again.
   - If the user says they are lost, confused, or asks what the introduced concepts mean, treat that as a failed initial explanation. Stop asking for a decision, discard the current framing, and rebuild it from the triggering situation and observable behavior. Do not merely define the same labels with more labels.
   - If the user asks a clarifying question, answer it without recording a decision or advancing. Re-present the choices only when useful.
   - If the user proposes a modification, restate only the revised decision, update the recommendation or example as needed, and wait for explicit acknowledgment before recording it.
8. When they clearly acknowledge a terminal choice (including terse approvals such as `agreed`, `approved`, `okay`, or `continue` when unambiguous), record the decision to the working doc if one is in scope, mark the task `completed`, and move to the next item.

Do not ask "continue or add more?" between items — assume continue unless the user volunteers something.

### Decision frame

Use this order by default:

1. **Situation** — explain the triggering event or user task, what the system must decide, and what goes wrong if it chooses incorrectly. Start in application/user behavior, not implementation terminology.
2. **How the choices behave** — explain every choice in terms of what happens. Introduce a type, API, or variant name only after its behavior is understood, and define it at that point. Mark every undecided name immediately as `(name TBD)`.
3. **Concrete scenario** — when the decision involves timing, lifecycle, ownership, state, or multiple actors, walk through one short example before asking the question.
4. **Question** — state the single decision in behavioral language. The user should not need to know the implementation names to answer it.
5. **Recommendation** — give the recommended outcome first and connect it to the user's stated goal or the concrete failure it avoids. Name meaningful costs or restrictions.
6. **Implementation mapping (optional)** — only after the behavior is understood, show the proposed type/API names or a small new-behavior code example when that helps record the decision precisely.
7. **Still pending** — name only nearby decisions that could otherwise look silently decided. Say where they will be handled when known. Do not dump the full backlog.
8. **Choices** — one plain-text line with exactly one recommendation and `elaborate`; describe behavior before labels.

Example shape:

```text
### A3 — <decision title>

When <triggering situation>, the application currently/proposedly ... . The
decision matters because ... .

The choices behave differently:
- With <behavioral choice>, the system ... .
- With <other behavioral choice>, the system ... .

The question is whether ...

I recommend ... because ...

In the implementation, this would be named `<ProposedType>` (name TBD).

Still pending:
- ... remains for A4.

approve (recommended — reason) / alternative / elaborate
```

This is a scaffold, not a demand for headings when a very small item reads more clearly in a few sentences.

### Behavior-first example

This does **not** introduce a decision:

```text
The recovery variants are `Disabled`, `ApplicationControlled`, and
`FallbackAndReturn`. Which should the primary use?
```

It assumes the names explain themselves. Introduce the same decision from what
the application does:

```text
If the editor's monitor disappears, the application can do one of three things:
- leave the editor absent;
- notify application code and wait for it to create a replacement; or
- create a temporary editor on another monitor and return it automatically when
  the original physical display reconnects.

For the main editor, should the application keep it usable automatically, wait
for application code, or leave it absent?
```

## Step 5: Wrap up

When every item is done:

- If a working doc was used: one-line summary — `Wrote decisions for N completed items to <path>`.
- Otherwise: summarize inline, one line per item — `1. <label> → <decision>`.
- Ensure every task has a terminal status.

## Rules

- **NEVER run the review as a survey.** No AskUserQuestion, no option chips, no questionnaire UI — for the items themselves or for any step of this workflow. Every item is a succinct, clear plain-text summary followed by the inline choice line defined in Step 4. Clear beats simple: keep the technical content, drop the widget.
- One item at a time. Never present two items in the same turn.
- Default presentation must be decision-ready on its first pass. `elaborate` adds depth; it is not an escape hatch for omitted foundational context.
- Do not mechanically fill headings and mistake that for context. A response can contain `Situation`, `Question`, and `Recommendation` sections and still fail if the causal relationship between them remains implicit.
- Never infer that the user knows a concept because they own the project, requested a technical review, or previously approved a related feature. Use only concepts already explained in user-facing conversation; introduce everything else behavior-first.
- Never introduce an unexplained name or substitute a code label for an explanation. Explain what it does first. If a name is undecided, label it `(name TBD)` instead of making it sound settled.
- Never present enum variants, policy names, state names, or API alternatives without saying what each one makes the system do.
- Keep the recommendation scoped to the active question. Put adjacent unresolved consequences in **Still pending** rather than deciding them implicitly.
- For technical changes, include enough current behavior to explain why the decision exists. Avoid unsolicited before/after code comparisons; this restriction never permits omitting the triggering situation.
- Whenever you present discrete choices — here or in Step 2 — always mark exactly one as recommended with a one-line reason. Open prompts ("any thoughts?") are exempt.
- Don't echo the user's feedback back to them unless asked — just record it.
- If the user says skip, use a supported skipped/cancelled terminal status; if none exists, mark it completed with the skip noted. Then move on without arguing.
- If they want to revisit an earlier item, jump back. Don't insist on linear order.
- Keep prompts concise only after they pass the comprehension gate. Missing causal context costs more than additional words.
- If the user asks for plain-English explanation of an item, give it without abandoning the one-at-a-time rhythm.
- If the review maintains a decision ledger, update it only after acknowledgment and leave undecided decision cells blank. Show it after each acknowledgment only when the user requested a running ledger; otherwise show it at wrap-up.
- When the user adds a later review item, record it without forcing an immediate decision; place it after any prerequisites they named.
