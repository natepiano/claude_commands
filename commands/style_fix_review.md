---
description: Review changes in a style-fix worktree against its EVALUATION.md findings
---

**Context:** You are in a `_style_fix` worktree created by the nightly automation. The nightly pipeline:
1. Ran `/style_eval` on the main branch, producing `EVALUATION.md` with numbered findings
2. Created this worktree on branch `refactor/style`
3. Launched Claude to apply every finding, run clippy, and run tests
4. Left the worktree with uncommitted changes for human review

**Your task:** Review the changes the automation made and determine whether each finding was correctly and completely addressed.

## Writing style for the review output

The user sees only what you write. They have **not** read the `EVALUATION.md` Fix Summary, the `cargo mend` output, the diff, or any style file. Every section of your review must stand on its own for a reader starting cold.

Follow these rules in every section you write (summary table, Cargo Mend section, Allow Audit, per-finding walkthrough):

- **Show the evidence before drawing a conclusion.** Quote the specific diff hunk, Fix Summary bullet, or style-guide line first, then interpret it. Never lead with a conclusion and expect the reader to back-derive the facts.
- **Introduce every term the first time it appears.** Tool names (`cargo mend`), lint names (`forbidden_pub_crate`, `redundant_closure`), internal concepts ("ux movable", "inspector switch"), and file-local identifiers (`tests/support.rs`) all need a one-clause gloss the first time you use them — even if they're obvious to you.
- **Attribute new information to its source out loud.** Never write "a known issue" or "as expected" — the user does not know what is known or expected. Write "The Fix Summary reports that..." or "`cargo mend` warns on this file because..." so the user can see where the claim came from.
- **Split multi-step inferences into separate sentences.** If a statement chains together "what the tool does" + "what the file contains" + "why the agent chose X", the reader cannot follow it. Give each fact its own sentence and name each one.
- **Avoid insider shorthand.** Do not write "mend-attributed edits" — write "edits the Fix Summary credits to `cargo mend --fix`". Do not write "the forbidden_pub_crate rule re-fails" — write "after mend rewrites `pub` to `pub(crate)`, a separate lint called `forbidden_pub_crate` then complains about that `pub(crate)`, so the fix cycles".
- **Prefer concrete over abstract.** When describing what the automation changed, name the actual before/after (e.g. "the `SetCursorExt` trait method `set_cursor` became a free function `cursor::set_cursor(&mut commands, cursor)`"), not a generalization ("the single-impl trait was replaced with a free function").
- **Don't assume the Fix Summary is trustworthy context for the user.** If a Fix Summary claim is load-bearing for your assessment, paraphrase what it says before using it — don't cite it by reference.

## Step 1: Read the evaluation and fix summary

Read `EVALUATION.md` in this worktree. It contains two parts:
1. **Findings** — the numbered style violations identified by `/style_eval`
2. **Fix Summary** — appended by the fix agent, documenting what it did, what it skipped, and why

Start by reading the Fix Summary section at the bottom. This gives you the agent's own account of what happened — which findings were applied, which were skipped or partially applied, and any issues encountered (build failures, pattern mismatches, style conflicts, etc.). Also look for the **Cargo Mend Changes** subsection, which documents any automatic visibility/import fixes made by `cargo mend --fix`. Use this as your starting point: you'll verify these claims against the actual diff.

## Step 2: Load the style guide and read referenced files

Run:

```bash
zsh ~/.claude/scripts/load-rust-style.sh
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

## Step 5: Review cargo mend changes

Check the Fix Summary for a **Cargo Mend Changes** section. If cargo mend made changes, write the review section for the user as follows.

**First, set the stage for the user.** Assume the user does not know what `cargo mend` is or what `--fix` did. Begin the Cargo Mend section with one or two plain sentences that explain: `cargo mend` is a workspace visibility/import auditor; `cargo mend --fix` applied automatic corrections (commonly narrowing `pub` to `pub(crate)`/`pub(super)` when nothing outside the crate/module needs the wider visibility, or rewriting imports to match the repo's import-style rules).

**Then, for each file the Fix Summary credits to mend:**

- Name the file and describe the concrete change by quoting or paraphrasing the diff (e.g. "`use crate::cursor::SetCursorExt;` became `use crate::cursor;` with call sites rewritten to `cursor::set_cursor(&mut commands, ...)`").
- State what that change type is (visibility narrowing, import-path rewrite, etc.) so the user can pattern-match across files.
- If the mend change overlaps with a numbered finding, say so explicitly: "This aligns with Finding N because <explanation of the connection>." Do not make the user infer the link.

**For any mend issue the Fix Summary flags as unfixable or cycling:**

- Quote the Fix Summary's description of the problem before interpreting it.
- Spell out in your own words the mechanism (e.g. "mend wants to narrow `pub` to `pub(crate)`, but a separate lint called `forbidden_pub_crate` is configured to reject `pub(crate)` on exported test helpers, so mend's fix is reverted on every run").
- State what the agent chose to do and why, concretely.
- Never describe this as "a known issue" without explaining *what* the issue is and *how you know* it (e.g. "The Fix Summary reports that...").

If the Cargo Mend Changes section says mend was skipped or found nothing, say so and move on — but in a full sentence that names what was skipped, not as shorthand.

## Step 6: Review each finding

For each numbered finding in EVALUATION.md, assess:

- **What was done** — Summarize the actual changes made for this finding (files touched, what was moved/renamed/rewritten)
- **Applied?** — Was the finding addressed in the diff?
- **Correct?** — Does the change match the recommended pattern and conform to the style guide? Is the transformation accurate?
- **Complete?** — Were all instances listed in "Scope" handled, or were some missed?
- **Side effects?** — Did the change introduce any bugs, break any patterns, or change behavior?
- **New allows?** — Did addressing this finding introduce any new allow that should be surfaced to the user?

## Step 7: Summary table

Output an overall summary table:

| # | Finding | Applied | Correct | Complete | Issues |
|---|---------|---------|---------|----------|--------|

Immediately after the summary table, output a short `Allow Audit` section that lists every new allow found in the diff, or explicitly says `No new allows found in diff.` if none were added.

## Step 8: Walk through each finding

Immediately after the summary table, begin presenting findings one at a time. For each finding, output:

1. The path to the relevant style guide file (from the finding's **Style file** field).
2. A **restatement of the original finding in plain language** — what style rule was violated, where it lived in the code, and what the evaluation asked for. Do not just say "Finding 2 was about single-impl traits" — name the traits, the files, and the recommended fix shape.
3. A **concrete description of what the automation changed**. Name the before and after by their actual identifiers. Example: "The trait `SetCursorExt` (declared in `cursor.rs`) and its single `impl SetCursorExt for Commands<'_, '_>` were deleted. In their place is a free function `cursor::set_cursor(commands: &mut Commands, cursor: Cursor)`. All call sites were rewritten from `commands.set_cursor(...)` to `cursor::set_cursor(&mut commands, ...)`." Do not collapse this into "the trait was replaced by a free function".
4. A **correctness and completeness assessment**, citing the style guide rule being applied and the specific places the fix landed or missed.
5. **Concerns, side effects, or risks**, each introduced as its own point with the evidence that raised the concern (diff quote, style rule, etc.) before the interpretation. If a concern is speculative, say so.
6. **Any new allow introduced while addressing this finding**, even if it includes a `reason` field.

Keep the prose tight but follow the writing-style rules at the top of this document — every term named, every claim evidenced, no shorthand.

Then **stop and wait for user feedback** before moving to the next finding. The user may have corrections, questions, or want you to fix something before proceeding.

If a finding is incomplete, incorrect, or otherwise needs follow-up changes, explicitly ask whether the user wants to fix it now or wait until the end of the review before continuing.

After the user responds (or says to continue), move to the next finding and repeat.

**Rules:**
- Do NOT make any changes — this is a read-only review
- Do NOT commit anything
- If the diff is empty (automation produced no changes), say so and note which findings were skipped
