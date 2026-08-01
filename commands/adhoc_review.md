---
description: Walk through decisions one at a time using behavior-first, decision-ready context, one atomic question, a recommendation, relevant pending decisions, and optional working-doc synchronization.
---

Use this when the user has just received a long list — recommendations, findings, options, todos — and wants to review them deliberately one by one instead of responding to the whole wall of text.

**Read `~/.claude/docs/user_facing_explanation.md` first and follow it.** It owns
the principle every rule here derives from — you do the reconstruction, not the
user — plus the build order, the naming rules, the banned vocabulary, the
comprehension gate, the choice-line format, and what to do when an explanation
fails to land. This file adds only what is specific to walking a list.

**Read `~/.claude/docs/type_design.md` too and follow it.** When an item concerns
types, APIs, stored state, or optional values, use that contract to evaluate the
proposal and recommendation. A vague name or domain-owned `Option<T>` is a
design issue; consider the restructuring needed to make a precise semantic type
truthful, not only a rename.

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

## Step 3.5: Establish the shared model

Run this once, before the first item, whenever the items all live inside one
model the user has not already been walked through in this conversation — a set
of types, actors, or states that only mean anything in relation to each other.
Skip it only when the items are genuinely independent of one another, or when the
user built the model with you earlier in the session.

**This is not a glossary.** A sequence of definitions is exactly what fails: each
one is individually correct and the reader still cannot say how the pieces fit,
so they reconstruct the model themselves out of N item-scoped explanations.
State the relationships instead.

Cover this, in this order:

1. **The participants** — one line each, giving the *question it answers* rather
   than what it is. "`DeviceKey` — which physical unit is this?" beats
   "`DeviceKey` is a durable device identifier."
2. **What is durable and what is transient.** Which participants outlive which,
   and what event makes a transient one go away. This is usually the load-bearing
   fact of the whole model and it is almost never stated outright.
3. **How they connect** — which participant points at which, with cardinality in
   both directions: "one device, many roles; one role, one device at a time."
4. **One worked instance** with real values, not placeholders. A table of
   concrete rows carries relationships better than prose — this is the one place
   in this command where a table is the right tool.
5. **One change over time.** Show that same instance before and after the event
   the model exists to survive — a device unplugged, a session lost, a job
   cancelled. What holds still and what moves *is* the model.

Then hand it back for correction:

> `That's the model the N items sit inside. Restate it your way or correct it, and I'll start the walk.`

Wait for the response. A correction here is worth more than a correction ten
items deep, because every later item inherits this framing. If the user restates
it differently but equivalently, adopt **their** wording for the rest of the walk
— theirs, not the source document's.

**Ask no decision question in this step.** Orientation and decisions are separate
turns.

## Step 4: Walk the list

For each item, in order:

1. Mark the task `in_progress`.
2. Check that the item contains **one decision**. If it contains independent questions, split them into ordered subitems, preserve the original ID as their prefix, update the task list, and present only the first.
3. **Build a decision-ready explanation before formatting the item**, following
   the build order and naming rules in the shared file. Two additions specific to
   list items:
   - Use one concrete scenario when timing, lifecycle, ownership, or state transitions create the decision.
   - Distinguish existing behavior from proposed behavior, but do not make the user reconstruct the causal story from implementation facts.
4. Apply the **comprehension gate** from the shared file before presenting. `elaborate` never supplies context the initial decision required.
5. Present the item with the decision frame below.
6. **Present the choices as an inline text line** per the shared file's Choices section — one line of message text, exactly one option marked recommended with its reason, never a survey or multiple-choice UI. Pick a choice set that fits the item (`keep / drop / modify`, `approve / reject / redirect`, …) and always include `elaborate`:
   > `keep (recommended — survived review unchanged) / modify / drop / elaborate`
7. **Wait for the user's response.** Do not move on until they reply.
   - If the user picks `elaborate` (also: "more", "detail", "expand", "why"): add one new rationale, constraint, or example relevant to the current item. Do not broaden scope or dump the raw item. Then re-present the same choices and wait again.
   - If the user says they are lost, confused, or asks what the introduced concepts mean, treat that as a failed initial explanation and stop asking for a decision. Apply repair-downward-then-upward from the shared file: **the upward repair here is Step 3.5** — run it now, whether or not it ran earlier. Re-running it mid-walk costs one turn; iterating item detail at ever finer grain costs many and does not terminate.
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

## Step 5: Wrap up

When every item is done:

- If a working doc was used: one-line summary — `Wrote decisions for N completed items to <path>`.
- Otherwise: summarize inline, one line per item — `1. <label> → <decision>`.
- Ensure every task has a terminal status.

## Rules

`~/.claude/docs/user_facing_explanation.md` carries the explanation rules — build
order, naming, banned vocabulary, comprehension gate, choice format, repair when
an item does not land. These are the ones specific to walking a list.

- **NEVER run the review as a survey.** No AskUserQuestion, no option chips, no questionnaire UI — for the items themselves or for any step of this workflow. Every item is a succinct summary followed by the inline choice line from Step 4. Keep the technical content, drop the widget.
- One item at a time. Never present two items in the same turn.
- **Model-level questions get model-level answers, at Step 3.5's altitude** — participants, what is durable versus transient, how they connect, one worked instance — and then return to the item. Adopt the user's wording verbatim if they restate the model themselves.
- Keep the recommendation scoped to the active question. Put adjacent unresolved consequences in **Still pending** rather than deciding them implicitly.
- Step 2's working-doc question is a choice like any other: mark exactly one option recommended.
- Avoid unsolicited before/after code comparisons. This never permits omitting the triggering situation.
- If the user says skip, use a supported skipped/cancelled terminal status; if none exists, mark it completed with the skip noted. Then move on without arguing.
- If they want to revisit an earlier item, jump back. Don't insist on linear order.
- An explanation request never suspends the one-at-a-time rhythm.
- If the review maintains a decision ledger, update it only after acknowledgment and leave undecided decision cells blank. Show it after each acknowledgment only when the user requested a running ledger; otherwise show it at wrap-up.
- When the user adds a later review item, record it without forcing an immediate decision; place it after any prerequisites they named.
