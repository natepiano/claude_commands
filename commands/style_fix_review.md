---
description: Review changes in a style-fix worktree against its EVALUATION.md findings
---

**Context:** You are in a `_style_fix` worktree created by the nightly automation. The nightly pipeline:
1. Ran `/style_eval` on the main branch, producing `EVALUATION.md` with numbered findings
2. Created this worktree on branch `refactor/style`
3. Launched Claude to apply every finding, run clippy, and run tests
4. Left the worktree with uncommitted changes for human review

**Your task:** Review the changes the automation made and determine whether each finding was correctly and completely addressed.

## Step 1: Load relevant style guide files

Each finding in EVALUATION.md includes a **Style file** field with the full path to the relevant style guide file (e.g., `~/rust/nate_style/rust/one-use-per-line.md`).

Read each unique style file referenced by the findings. These are your authoritative references for evaluating whether the changes conform to the style rules.

## Step 2: Read the evaluation

Read `EVALUATION.md` in this worktree. Note each numbered finding — what it asked for, which files it cited, and the recommended pattern.

## Step 3: Read the diff

Run `git diff` to see all unstaged changes. If there are also staged changes, run `git diff --cached` as well.

## Step 4: Review each finding

For each numbered finding in EVALUATION.md, assess:

- **What was done** — Summarize the actual changes made for this finding (files touched, what was moved/renamed/rewritten)
- **Applied?** — Was the finding addressed in the diff?
- **Correct?** — Does the change match the recommended pattern and conform to the style guide? Is the transformation accurate?
- **Complete?** — Were all instances listed in "Scope" handled, or were some missed?
- **Side effects?** — Did the change introduce any bugs, break any patterns, or change behavior?

## Step 5: Report

For each finding, provide:

### Finding N: [Title]
**What was done:** [1-2 sentence summary of the actual changes]
**Verdict:** Applied / Correct / Complete (or note what's missing)
**Issues:** [any problems, or "None"]

Then provide an overall summary table:

| # | Finding | Applied | Correct | Complete | Issues |
|---|---------|---------|---------|----------|--------|

Finally, list any issues that need attention before this can be merged — or state that it's ready.

**Rules:**
- Do NOT make any changes — this is a read-only review
- Do NOT commit anything
- If the diff is empty (automation produced no changes), say so and note which findings were skipped
