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

The user sees only what you write. They have **not** read the `EVALUATION.md` Fix Summary, the `cargo mend` output, the diff, or any style file. Your job is to surface what they *don't* know — without padding out what they already do know.

**Trust the reader on common ground.** Assume the user knows:
- Standard Rust and Cargo tooling (`cargo`, `clippy`, `cargo mend`, `rustfmt`, `nextest`)
- Common clippy lint names (`redundant_closure`, `single_match`, etc.)
- Basic Rust concepts (traits, visibility modifiers, closures, attributes)
- The repo layout and its own style guide

Do **not** re-introduce these. No "cargo mend is a workspace auditor..." preamble, no explaining what `#[allow]` does.

**The user does not know** (and you must surface explicitly):
- What the Fix Summary actually says — quote or paraphrase, don't reference by name
- Project-specific lints, configs, or files (`forbidden_pub_crate`, `tests/support.rs` helpers) the first time they appear
- Cross-tool interactions (e.g. mend's fix being reverted by another lint)
- Any claim of a "known issue" or "expected limitation"

**Formatting rules:**
- Use concise one-sentence answers when one sentence is enough. "No new allows in the diff." is a complete Allow Audit.
- Use **tables** for repetitive mechanical changes (e.g. multiple files with the same before/after shape) — before/after columns beat bullet-point prose every time.
- For a multi-step mechanism (lint A fights lint B, etc.), use: one intro sentence + numbered steps + a one-line recommendation. Not paragraphs.
- Don't forward-reference findings by number before the reader has read them in the walkthrough. If a mend change is tied to a later finding, describe the mechanical change without naming the finding — the connection surfaces naturally when they reach it.
- **Show evidence before interpretation** when the claim is non-obvious. If it's obvious (e.g. "trait removed, import rewritten to match"), skip the evidence preamble.

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

**Follow `see_also` cross-references.** When a style file's frontmatter has a `see_also:` field (a wikilink like `"[[other-file]]"` or a list of them), read those referenced files too and treat them as additional context while judging the finding. The loader output already appends see_also'd content under a `### Related style guidance (via see_also → ...)` subheading, so if you loaded from the loader you have it already; if you're reading the style file directly, open the see_also'd files manually. Do not raise the see_also'd rule as a separate concern — it shapes how you interpret the primary rule, not a standalone check.

## Step 3: Read the diff

Run `git diff` to see all unstaged changes. If there are also staged changes, run `git diff --cached` as well.

## Step 4: Audit new `allow`s in the diff

Inspect the diff for any newly added suppressions: `#[allow(...)]`, `#![allow(...)]`, or lint-table entries set to `"allow"` in `Cargo.toml` or workspace lints. A `reason = ...` does not exempt an allow from being reported.

**Output format:**

- If none found: one sentence — `No new allows in the diff.` Done.
- If any found: a short list, one per allow, with: `file:line` — lint name — `reason` (or `(none)`) — whether the style guide pre-authorizes it — a one-phrase note on whether it looks avoidable.

Consult `agent-must-review-allows.md`, `never-bare-allowdeadcode.md`, `cargo-toml-lints.md`, `cargo-toml-bevy-lints.md`, and any finding-specific style file to judge pre-authorization. Do not explain the methodology in the output — just report.

## Step 5: Review cargo mend changes

Check the Fix Summary for a **Cargo Mend Changes** section. Do **not** introduce what `cargo mend` is — the user already knows.

**If mend-credited edits exist in the diff:**

Summarize them as a before/after table. Group files that share the same before/after shape into a single row (with files listed in the `file` column). Do **not** cross-reference numbered findings here — the reader hasn't read them yet. Just describe what mechanically changed.

| file(s) | before | after |
|---------|--------|-------|
| ... | ... | ... |

Follow the table with at most one sentence classifying the overall shape (e.g. "All six are import-path rewrites; none narrowed visibility.").

**If the Fix Summary reports an unfixable or cycling mend item:**

Use this format — intro sentence, numbered sequence, one-line recommendation:

> **`<file>` cycles:** `<one-sentence description>`.
> 1. `<step one>`
> 2. `<step two>`
> 3. `<step three>`
>
> Agent left it as `<current state>`. Follow-up: `<single-line recommendation>`.

Name any project-specific lint the first time it appears (one clause is enough — e.g. "`forbidden_pub_crate` (repo-configured lint that rejects `pub(crate)` on test helpers)").

**If mend made no changes or was skipped:**

One sentence. Done.

## Step 6: Review each finding

For each numbered finding in EVALUATION.md, assess:

- **What was done** — Summarize the actual changes made for this finding (files touched, what was moved/renamed/rewritten)
- **Applied?** — Was the finding addressed in the diff?
- **Correct?** — Does the change match the recommended pattern and conform to the style guide? Is the transformation accurate?
- **Complete?** — Were all instances listed in "Scope" handled, or were some missed?
- **Side effects?** — Did the change introduce any bugs, break any patterns, or change behavior?
- **New allows?** — Did addressing this finding introduce any new allow that should be surfaced to the user?

## Step 7: Output order

Output in this exact order, then stop at the end of Finding 1:

1. Summary table with columns: `# | Finding | Applied | Correct | Complete | Issues`
2. `## Allow Audit` (per Step 4 format)
3. `## Cargo Mend Changes` (per Step 5 format)
4. `## Finding 1` walkthrough (per Step 8 format), then **stop and wait**

## Step 8: Walk through each finding

For each finding, output a compact block with these parts. Keep the whole thing tight — a paragraph or two plus short lists is usually enough.

1. **Style file path** — one line, from the finding's `Style file` field.
2. **Original issue** — one to two sentences naming the actual identifiers (traits, files, fields) that were flagged and what the evaluation asked for. Not "Finding 2 was about single-impl traits" — name them.
3. **What changed** — concrete before/after for the main edits. Use a table if there are four or more parallel call-site rewrites; otherwise short prose naming the before and after identifiers.
4. **Assessment** — a single line or short sentence: applied / correct / complete, and one phrase citing the style rule.
5. **Concerns** — bullet list only if there are any. Each bullet: the concern + the evidence (diff snippet, style-guide phrase) that raised it. Say "speculative" if it is.
6. **New allows** — one line. `None.` if none.

Then **stop and wait for user feedback** before moving to the next finding. The user may have corrections, questions, or want you to fix something before proceeding.

If a finding is incomplete, incorrect, or otherwise needs follow-up changes, explicitly ask whether the user wants to fix it now or wait until the end of the review before continuing.

After the user responds (or says to continue), move to the next finding and repeat.

**Rules:**
- Do NOT make any changes — this is a read-only review
- Do NOT commit anything
- If the diff is empty (automation produced no changes), say so and note which findings were skipped
