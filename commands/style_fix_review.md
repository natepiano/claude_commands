---
description: Review changes in a style-fix worktree against its EVALUATION.md findings
---

**Context:** You are in a `_style_fix` worktree created by the nightly automation. The nightly pipeline:
1. Ran `/style_eval` on the main branch, producing `EVALUATION.md` with numbered findings
2. Created this worktree on branch `refactor/style`
3. Launched Claude to apply every finding, run clippy, and run tests
4. Left the worktree with uncommitted changes for human review

**Your task:** Review the changes the automation made and determine whether each finding was correctly and completely addressed.

## Step 1: Read the evaluation and fix summary

Read `EVALUATION.md` in this worktree. It contains two parts:
1. **Findings** — the numbered style violations identified by `/style_eval`
2. **Fix Summary** — appended by the fix agent, documenting what it did, what it skipped, and why

Start by reading the Fix Summary section at the bottom. This gives you the agent's own account of what happened — which findings were applied, which were skipped or partially applied, and any issues encountered (build failures, pattern mismatches, style conflicts, etc.). Use this as your starting point: you'll verify these claims against the actual diff.

## Step 2: Load the style guide and read referenced files

Run:

```bash
bash ~/.claude/scripts/load-rust-style.sh
```

Then read each unique style file referenced by the findings. Each finding in EVALUATION.md includes a **Style file** field with the full path to the relevant style guide file (e.g., `~/rust/nate_style/rust/one-use-per-line.md` or a repo-local `docs/style/*.md` file).

The referenced files are your authoritative sources for evaluating whether the changes conform to the style rules.

## Step 3: Read the diff

Run `git diff` to see all unstaged changes. If there are also staged changes, run `git diff --cached` as well.

## Step 4: Audit new `allow`s in the diff

Before reviewing the numbered findings, explicitly inspect the diff for any newly added suppressions. This is mandatory even if the allow has a `reason`.

Check for:
- New `#[allow(...)]` or `#![allow(...)]` attributes
- New lint config entries set to `"allow"` in `Cargo.toml` or workspace lint tables
- Any other newly added allow-based suppression mechanism visible in the diff

For every new allow you find:
- Report the file and location
- Name the lint or rule being allowed
- Include whether a `reason` is present
- State whether the style guide explicitly pre-authorizes it
- Explain whether the change appears avoidable via removal or restructuring
- Flag it to the user as a review item even if it might be justified

Use the style guide as the source of truth here, especially `agent-must-review-allows.md`, `never-bare-allowdeadcode.md`, `cargo-toml-lints.md`, `cargo-toml-bevy-lints.md`, and any finding-specific style file. Do not treat a `reason = ...` as sufficient by itself. The review must still call out the new allow and put it in front of the user.

If there are no new allows in the diff, say so explicitly.

## Step 5: Review each finding

For each numbered finding in EVALUATION.md, assess:

- **What was done** — Summarize the actual changes made for this finding (files touched, what was moved/renamed/rewritten)
- **Applied?** — Was the finding addressed in the diff?
- **Correct?** — Does the change match the recommended pattern and conform to the style guide? Is the transformation accurate?
- **Complete?** — Were all instances listed in "Scope" handled, or were some missed?
- **Side effects?** — Did the change introduce any bugs, break any patterns, or change behavior?
- **New allows?** — Did addressing this finding introduce any new allow that should be surfaced to the user?

## Step 6: Summary table

Output an overall summary table:

| # | Finding | Applied | Correct | Complete | Issues |
|---|---------|---------|---------|----------|--------|

Immediately after the summary table, output a short `Allow Audit` section that lists every new allow found in the diff, or explicitly says `No new allows found in diff.` if none were added.

## Step 7: Walk through each finding

Immediately after the summary table, begin presenting findings one at a time. For each finding, start by outputting the path to the relevant style guide file (from the finding's **Style file** field), then a short narrative (3-5 sentences) covering:
- A brief re-summary of the original finding itself so the user has context before your review. Restate the core issue, the requested style change, and the relevant scope/locations in 1-2 sentences before discussing what the automation did.
- What the automation actually changed
- Whether it was applied correctly and completely
- Any issues or concerns
- Any new allow introduced while addressing this finding, even if it includes a reason

Then **stop and wait for user feedback** before moving to the next finding. The user may have corrections, questions, or want you to fix something before proceeding.

If a finding is incomplete, incorrect, or otherwise needs follow-up changes, explicitly ask whether the user wants to fix it now or wait until the end of the review before continuing.

After the user responds (or says to continue), move to the next finding and repeat.

**Rules:**
- Do NOT make any changes — this is a read-only review
- Do NOT commit anything
- If the diff is empty (automation produced no changes), say so and note which findings were skipped
