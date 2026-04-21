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

## Hard rule: never apply without explicit approval

After proposing a fix or change, stop. Do not run Edit, Write, Bash (for mutation), git operations, or any tool that changes the repository until the user has replied with an explicit go-ahead ("approve", "apply", "yes", "do it", etc.).

This rule applies to **every** phase of this skill — the initial finding walkthrough, and the follow-up task list that the user may create after the walkthrough. There is no phase in which self-approval is acceptable.

- Asking follow-up questions is fine; applying based on my own answers to those questions is not.
- `auto mode`, `/auto`, and similar global runtime flags do not override this rule. The per-task "propose → wait → apply → stop" rhythm comes from explicit user instruction and is the stronger signal.
- Read-only tools (Read, Glob, Grep, Bash for inspection with no write side-effects) are allowed between proposal and approval — they sharpen the proposal, not execute it.
- A user response that is a clarification or question (not an approval) restarts the cycle: refine the proposal and stop again.
- If I catch myself starting to reach for Edit/Write before the user has explicitly approved, stop the chain, do not submit the tool call, and instead re-state the proposal and wait.

## Hard rule: one finding per response

This skill is a strictly sequential walkthrough. After the initial header output (summary table + Allow Audit + Cargo Mend Changes, then Finding 1 **only if the Allow Audit is clean** — see "Allow Audit halt" below), every subsequent response contains **exactly one** finding walkthrough and then stops.

- When the user replies with *any* continuation signal ("continue", "next", "ok", "go", "yes", "keep going", etc.), advance by exactly one finding. Never two. Never "the rest."
- Do not output an end-of-review summary or queued-task list until the user has seen and responded to each individual finding.
- If the remaining findings look mechanical, trivial, or redundant — still output them one at a time. The user is pacing the review, not you.
- This rule overrides any instinct toward "efficiency" or "wrapping up."

## Hard rule: read the governing style file before every fix proposal

Every fix proposal must be preceded by reading the governing style file and quoting its prescription. Do not brainstorm options when the style guide has already ruled on the case.

- **Follow-up tasks derived from a finding** — the governing file is the finding's `Style file:` field in `EVALUATION.md`. Read it; cite it.
- **Follow-up tasks tied to a clippy lint** — the governing file is whichever style doc has the lint name in its frontmatter `clippy:` field. Find it by grepping the loaded style files for the lint name.
- **Every proposal must start with a `**Style rule:**` line** quoting the relevant prescription, or stating "no specific rule; proposing by analogy to X."

If you find yourself listing options (a)/(b)/(c) before citing the rule, stop and do the lookup first. The rule often names the answer.

### Secondary concerns within a fix

A single fix may touch concerns the finding's style file does not cover — e.g. a module split that incidentally needs to move an `#[allow]`, or an API change that raises a naming question. Treat each secondary concern as its own lookup:

1. **Identify every independent style concern the fix involves** before drafting the proposal. Module boundaries, lint suppressions, naming, visibility, comments, observers, error handling — each is typically governed by a separate style file.
2. **For each concern, find the governing file:**
   - If it's a clippy lint, grep for the lint name in loaded style files.
   - If it's a pattern (visibility, module shape, observers, etc.), grep for the domain keywords or scan filenames for a match.
   - If nothing matches, state that explicitly in the proposal: "no style rule found for <concern>; proposing <choice> because <reasoning>."
3. **Cite each governing file** with a `**Style rule (<concern>):**` line in the proposal. A fix touching three concerns should cite three rules (or explicitly note which have no governing rule).
4. **Never propose a pattern for a secondary concern without doing the lookup**, even if the concern feels minor. A one-line allow placement or a two-word rename can violate an explicit rule just as loudly as a big restructure.

## Hard rule: Allow Audit halt

If the Allow Audit surfaces **any** new allow that is not pre-authorized by the style guide, the skill **must halt after the Allow Audit section** — before Cargo Mend Changes, before Finding 1. New allows are a direct violation of `agent-must-review-allows.md` and must be resolved with the user before the rest of the review proceeds.

- After the Allow Audit, add a short `## Recommendations` section listing each flagged allow with a concrete proposed action (remove, restructure, relocate to `mod` declaration, etc.).
- End the response with an explicit question asking the user how they want to handle the flagged allow(s) before continuing the review.
- Do **not** output Cargo Mend Changes or Finding 1 in the same response.
- Only proceed with Cargo Mend Changes + Finding 1 in the next response, after the user has directed how to resolve (or accept) each flagged allow.
- If the Allow Audit is `No new allows in the diff.`, this halt does not apply — continue to Cargo Mend Changes + Finding 1 as usual.

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

- If none found: one sentence — `No new allows in the diff.` Done. Continue to Step 5.
- If any found: a short list, one per allow, with: `file:line` — lint name — `reason` (or `(none)`) — whether the style guide pre-authorizes it — a one-phrase note on whether it looks avoidable.

Consult `agent-must-review-allows.md`, `never-bare-allowdeadcode.md`, `cargo-toml-lints.md`, `cargo-toml-bevy-lints.md`, and any finding-specific style file to judge pre-authorization. Do not explain the methodology in the output — just report.

**If any non-pre-authorized allow was found, halt after this section.** Per the "Allow Audit halt" rule above:

1. Append a `## Recommendations` section. For each flagged allow, give one concrete proposed action (e.g. "remove and re-run clippy to see if still needed", "move to `#[allow(...)] mod foo;` on the parent `mod` line per `used-underscore-binding-module-level-allow-only.md`", "restructure the call site to eliminate the lint").
2. End the response with a single direct question asking how the user wants to handle the flagged allow(s).
3. Do **not** output Cargo Mend Changes or Finding 1 in the same response — those come in the next turn after the user responds.

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

The first response branches based on whether the Allow Audit is clean.

**Case A — Allow Audit is clean (`No new allows in the diff.`):**

1. Summary table with columns: `# | Finding | Applied | Correct | Complete | Issues`
2. `## Allow Audit` (per Step 4 format — one sentence)
3. `## Cargo Mend Changes` (per Step 5 format)
4. `## Finding 1` walkthrough (per Step 8 format)

Then **stop**. Do not output Finding 2 or anything else in the same response. Wait for the user to reply before producing Finding 2 — and then Finding 2 only.

**Case B — Allow Audit flagged one or more non-pre-authorized allows:**

1. Summary table with columns: `# | Finding | Applied | Correct | Complete | Issues`
2. `## Allow Audit` (per Step 4 format — itemized list)
3. `## Recommendations` (one concrete proposed action per flagged allow)
4. A direct question asking the user how to proceed.

Then **stop**. Do **not** output Cargo Mend Changes or Finding 1 in this response. Once the user has resolved the allow question in a subsequent turn, the next response outputs:

1. `## Cargo Mend Changes` (per Step 5 format)
2. `## Finding 1` walkthrough (per Step 8 format)

— and then stops, following the one-finding-per-response rule.

## Step 8: Walk through each finding

For each finding, output a compact block with these parts. Keep the whole thing tight — a paragraph or two plus short lists is usually enough.

1. **Style file path** — one line, from the finding's `Style file` field.
2. **Original issue** — one to two sentences naming the actual identifiers (traits, files, fields) that were flagged and what the evaluation asked for. Not "Finding 2 was about single-impl traits" — name them.
3. **What changed** — concrete before/after for the main edits.
   - **Always anchor edits to file paths.** Every "what changed" claim must cite the file the change lives in. This is non-negotiable: without a path, the reader has no mental map of the diff.
   - **Format decision rule.** Use a table *only* when each before/after cell fits on one line of real code with no ellipses and no prose. If the change spans multiple lines or reshapes structure, use a fenced code block with `Before:` / `After:` headers. If the change can't be shown as code (file moves, deletions, renames of whole files), use a prose line with the path.
   - **Anti-pattern.** If a before/after cell contains English describing what the code did (e.g. "returning `Option` via the match", "two early branches followed by the fall-through tuple"), the format is wrong — promote to a code block. Cells hold code, not commentary.
   - **Tables** — include a `file` column as the first column. Every row must name the file(s) affected. Files go in their own column, not embedded in before/after cells. Example:

     | file | before | after |
     |---|---|---|
     | `src/fit.rs` | `is_ortho: bool` | `mode: ProjectionMode` |
   - **Code blocks** — use for multi-line or structural changes. Example:

     `src/fit.rs` (`calculate_target_margins`):
     ```rust
     // Before
     if vertical_extent < THRESHOLD {
         return (a, b);
     }
     if horizontal_extent < THRESHOLD {
         return (c, d);
     }
     (e, f)
     ```
     ```rust
     // After
     if vertical_extent < THRESHOLD {
         (a, b)
     } else if horizontal_extent < THRESHOLD {
         (c, d)
     } else {
         (e, f)
     }
     ```
   - **Prose form** — use for file moves, splits, deletions, or renames of whole files. Pattern: `` `old/path.rs` → `new/path.rs` `` with a one-clause description of why. Never write "X was renamed to Y" without paths.
4. **Assessment** — a single line or short sentence: applied / correct / complete, and one phrase citing the style rule.
5. **Concerns** — bullet list only if there are any. Each bullet: the concern + the evidence (diff snippet, style-guide phrase) that raised it. Say "speculative" if it is.
6. **New allows** — one line. `None.` if none.

Then **stop the response**. Do not preview the next finding. Do not list upcoming findings. Do not write an end-of-review summary.

The user may have corrections, questions, or want you to fix something before proceeding. If a finding is incomplete, incorrect, or otherwise needs follow-up changes, explicitly ask whether the user wants to fix it now or wait until the end of the review before continuing.

When the user's next message arrives, produce exactly one finding walkthrough — the next numbered one — and stop again. Repeat until the user has seen every finding individually.

**Rules:**
- Do NOT make any changes — this is a read-only review
- Do NOT commit anything
- If the diff is empty (automation produced no changes), say so and note which findings were skipped
