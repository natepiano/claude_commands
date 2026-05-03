---
description: Walk through a list of items (typically just presented by another agent) one at a time for user feedback, optionally syncing decisions into a working doc.
---

Use this when the user has just received a long list — recommendations, findings, options, todos — and wants to review them deliberately one by one instead of responding to the whole wall of text.

## Step 1: Identify the items

Find the list to review. Look in this order:

1. The user's invocation contains the list (pasted in, or pointing at a section).
2. The most recent assistant message had an enumerated list — bullets, numbered items, table rows.
3. Neither — ask the user where the list is.

Reply with a short count: `Found N items to review.` Do not list them yet.

## Step 2: Set up where decisions get recorded

Decide whether there is a working doc in this conversation — a file path the user has been editing or named this session.

Then ask **one** question, picking the form that matches the situation:

- **A doc is already in scope:** name it.
  > `N items. Record decisions in <relative/path/to/doc.md>? (yes / different path / none)`
- **No doc is in scope:** say so.
  > `N items. No working doc in this conversation. Record decisions in a doc? (path to use / suggest one / none)`

Rules:
- Never use the placeholder phrase "existing path" — either name the doc or say there isn't one.
- If the user gives a path that doesn't exist, create it with a one-line header (today's date + where the items came from).
- If the user picks `none`, skip the doc-writing step in Step 4 and just summarize at the end.

## Step 3: Build the todo list

If a todo tool is available (e.g. `TaskCreate`), add one task per item, in order, each with a short label and status `pending`. If not, keep an inline numbered checklist and refer to items by number.

## Step 4: Walk the list

For each item, in order:

1. Mark the task `in_progress`.
2. **Quote the item verbatim.** Add context only if the item references something not contained in itself.
3. **End with a short, clear prompt for feedback.** Pick one that matches the item — examples: `Keep, drop, or modify?` / `Skip or dig in?` / `Any thoughts?` / a question tailored to the item. Avoid one-word jargon prompts like `Take?` — terse is good, cryptic is not.
4. **Wait for the user's response.** Do not move on until they reply.
5. When they signal continue (any reply that isn't "stop" or "go back"), record the decision to the working doc if one is in scope, mark the task `completed`, and move to the next item.

Do not ask "continue or add more?" between items — assume continue unless the user volunteers something. Late additions before the next doc-write are captured naturally.

## Step 5: Wrap up

When every item is done:

- If a working doc was used: one-line summary — `Wrote N decisions to <path>`.
- Otherwise: summarize inline, one line per item — `1. <label> → <decision>`.
- Mark all tasks `completed`.

## Rules

- One item at a time. Never present two items in the same turn.
- Don't echo the user's feedback back to them unless asked — just record it.
- If the user says skip, mark the task `cancelled` and move on. Don't argue.
- If they want to revisit an earlier item, jump back. Don't insist on linear order.
- Keep prompts terse but readable. Every extra word costs them — but so does cryptic shorthand.
- If the user asks for plain-English explanation of an item, give it without abandoning the one-at-a-time rhythm.
