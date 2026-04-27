---
description: Walk through a list of items (typically just presented by another agent) one at a time for user feedback, optionally syncing decisions into a working doc.
---

Use this when the user has just received a long list — recommendations, findings, options, todos — and wants to review them deliberately one by one rather than respond to the wall of text.

## Step 1: Identify the items

Before doing anything else, identify the list to review. Sources, in order of preference:

1. The user's invocation provided the list explicitly (e.g. they pasted it or pointed at a section above).
2. The most recent assistant message in this conversation contained an enumerated list — bullets, numbered items, table rows.
3. If neither is true, ask the user where the items are.

Echo back a short count: "Found N items to review." Do not list them yet.

## Step 2: Establish the working doc (or skip)

Check whether a working doc is already in scope for this conversation — a path the user has been editing or named this session.

Ask one combined question:

> "N items. Doc to record decisions in? (existing path, suggest one, or `none`)"

Wait for the answer. If they give a path that doesn't exist, create it with a one-line header noting the date and the source of the items.

## Step 3: Build the todo list

If a harness todo tool is available (e.g. `TaskCreate`), add one task per item in order — each task name a short label, all `pending`. If no todo tool is available, track the list inline as a numbered checklist in your own notes and reference items by number.

## Step 4: Walk the list

For each item, in order:

1. Mark the task `in_progress` (or note position in your inline list).
2. **Quote the item verbatim.** Add context only if the item references something not contained in itself. End with a short prompt for feedback ("Take?" / "Keep, drop, modify?" / a question tailored to the item's shape).
3. **Wait for the user's feedback.** Do not move on until they respond.
4. Once the user signals to continue (their next message after feedback, or an explicit "next"), append the decision to the working doc if one is in scope, then mark the task `completed` and move to the next item.

Do not ask "continue or add more?" between items — assume continue unless the user volunteers more. Late additions before the doc-write step will be captured; that's the point of writing after the continue signal, not before.

## Step 5: Wrap up

When all items are done:

- If a working doc was used, summarize what got written (one line — file path + count of decisions captured).
- Otherwise, summarize the decisions inline (one line per item: "1. <label> → <decision>").
- Mark all tasks completed.

## Rules

- One item at a time. Never present two items in the same turn.
- Don't summarize the user's feedback back to them unless they ask — just record it.
- If the user wants to skip an item, mark its task `cancelled` and move on; don't argue.
- If they want to revisit an earlier item, jump back; don't insist on linear order.
- Keep prompts terse — the user is reviewing a long list, every extra word costs them.
