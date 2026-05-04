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

## Hard rule: the last finding stops like every other finding

The final finding is **not special**. It gets the same treatment as every other finding: walkthrough, then stop. No end-of-review summary, no recap table, no "all 5 findings have been walked," no follow-up task list, no "want me to do anything before you commit?" — *nothing* in the same response as Finding N's walkthrough.

The model's instinct will be to "wrap up" because there are no more findings. Resist it. The user has just spent N turns reading findings one at a time, deliberately pacing the review. Appending a summary in the same response as the last finding scrolls the last finding off-screen and dumps cognitive load on top of fresh content — exactly the failure mode the one-finding-per-response rule exists to prevent.

After the last finding's walkthrough, end with **one short closing line** that explicitly asks whether the user wants the end-of-review summary:

> *"That's the last finding. Want the end-of-review summary, or are we done?"*

Then stop. Do not produce the summary in the same response under any circumstances. The summary appears only in the *next* response, and only if the user asks for it.

This rule applies regardless of:
- whether `auto mode` is active
- whether previous findings looked similar to this one
- whether the user said "continue" with no further instruction
- whether the changes were trivial
- whether you feel the wrap-up would be brief

If you find yourself starting to write "End of review" or a recap table in the same turn as Finding N, stop, delete it, and replace it with the closing question above.

## Hard rule: read the governing style file before every fix proposal

Every fix proposal must be preceded by reading the governing style file and quoting its prescription. Do not brainstorm options when the style guide has already ruled on the case.

- **Follow-up tasks derived from a finding** — the governing file is the finding's `Style file:` field in `EVALUATION.md`. Read it; cite it.
- **Follow-up tasks tied to a clippy lint** — the governing file is whichever style doc has the lint name in its frontmatter `lint:` field. Find it by grepping the loaded style files for the lint name.
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

## Hard rule: do the style-rule lookup before every Concern bullet

This is the review-time mirror of the "read the governing style file before every fix proposal" rule. It applies to every Concern bullet you write during a finding walkthrough, including bullets about details the finding itself did not raise (visibility, imports, mod declarations, naming, allows, observers, error handling, etc.).

**The lookup is mandatory. The way the lookup appears in the bullet is not a prefix.** Before writing any Concern bullet, you must have performed one of:

- Found the governing style file and identified the relevant prescription, or
- Searched and found nothing applicable (record which files you grepped and which lint names you looked up).

If you cannot do one of those two things after a real lookup, delete the bullet. "It feels off" is not a Concern; it is a prompt to do the lookup or drop the worry.

**How the lookup shows up in the bullet output:** as a single short inline tag, not a leading preamble. See "Required format per concern" below for the exact template — typically `(rule: <file.md>)` or `(no rule found)` placed inside the bullet's body as a parenthetical on the statement sentence. Never as a `**Style rule:**` block that competes with the headline. The lookup is process; the citation is a one-token tag.

A Concern without an internally-completed lookup is a defect — it leaks bad guidance into the user's review of every future style fix. A Concern that ships with a `**Style rule:**` paragraph preamble is also a defect — it violates the format and buries the recommendation under prose.

### Visibility-trigger checklist

If the diff contains **any** of the following, you must read the listed files before writing any Concern that touches visibility, module structure, or import paths:

- a new or changed `pub(...)` modifier
- a new `mod` declaration (whether `pub`, `pub(super)`, `pub(crate)`, or private)
- a new or changed `pub use` / `pub(super) use` / `pub(crate) use` re-export
- a new leaf file under an existing module directory

Required reading on those triggers:

- `~/rust/nate_style/rust/leaf-module-visibility.md`
- `~/rust/nate_style/rust/no-pub-in-path.md`
- `~/rust/nate_style/rust/no-pubcrate-in-nested-modules.md`

Read them even when the finding's own `Style file:` points elsewhere. The mend loader includes these files (they are `mode: flag`, not `mode: auto`), but inclusion in the loader does not guarantee you consulted them — this checklist forces the consult.

## Hard rule: Allow Audit halt

If the Allow Audit surfaces **any** new allow that is not pre-authorized by the style guide, the skill **must halt after the Allow Audit section** — before Cargo Mend Changes, before Finding 1. New allows are a direct violation of `agent-must-review-allows.md` and must be resolved with the user before the rest of the review proceeds.

- After the Allow Audit, add a short `## Recommendations` section listing each flagged allow with a concrete proposed action (remove, restructure, relocate to `mod` declaration, etc.).
- End the response with an explicit question asking the user how they want to handle the flagged allow(s) before continuing the review.
- Do **not** output Cargo Mend Changes or Finding 1 in the same response.
- Only proceed with Cargo Mend Changes + Finding 1 in the next response, after the user has directed how to resolve (or accept) each flagged allow.
- If the Allow Audit is `No new allows in the diff.`, this halt does not apply — continue to Cargo Mend Changes + Finding 1 as usual.

## Hard rule: banned vocabulary in review output

This rule applies to **every section the user reads** — Original issue, What changed, Assessment, Implications, Concerns, and any narration around the summary table. Not just Concerns/Implications.

**Banned guide-jargon** (from the style guide; opaque to a reader who hasn't reread the docs today):

`domain cohort`, `domain-cohort`, `domain-noun cohort`, `behavior owner`, `dictionary file`, `data dictionary`, `junk drawer`, `cohort name`.

**Banned metaphor verbs and adjectives** (vague, decorative, tell the reader nothing concrete):

`carve`, `carves`, `carved`, `carving`, `carve-out`, `carve out`, `sculpted`, `woven`, `threaded`, `surfaced` (as a transitive verb meaning "exposed"), `crystallized`, `distilled`, `pipeline-shaped`, `module-shaped`, anything-`-shaped`, anything-`-flavored`. The word `shape` itself is already banned project-wide (see global writing rules) — do not slip it in here.

**`carve` and its inflections are a hard ban with zero exceptions.** This includes figurative uses like "the rule does not carve an exception" or "the style file carves out a subset." Never substitute another metaphor — use the literal verb that names the operation: `extract`, `split`, `move`, `refactor`, `introduce`, `define`, `permit`, `exempt`, `name as an exception`. Before submitting any response, grep your draft for `carv` as a substring and rewrite every hit.

If a banned guide term appears, it must be translated in the same sentence into concrete codebase nouns (file names, type names, function names) and a thing the user can do or decide. If a banned metaphor verb appears, replace it with the literal verb (`split`, `grouped`, `organized`, `divided`, `extracted`, `moved into`).

**Canonical delete-on-sight examples** (do not ship anything resembling these):

1. *"`controller.rs` is a domain-noun cohort name rather than an anchor type name."*
   `domain-noun cohort` is pure guide-jargon, nothing actionable. Either drop the bullet, or rewrite concretely: *"`controller.rs` holds the `orbit_cam` runtime system plus its private input/transform helpers — one weight-bearing item, not several peers. A name like `runtime.rs` or `pipeline.rs` would predict the contents better."*

2. *"The restore subtree was carved by pipeline phase (`apply/bootstrap.rs`, `apply/cross_dpi.rs`, …)."*
   `carved` is a decorative verb that says nothing the file list doesn't already say. Rewrite: *"The restore subtree is split into one file per pipeline phase: `apply/bootstrap.rs`, `apply/cross_dpi.rs`, …"* — or just *"The restore subtree has one file per pipeline phase: …"*.

Before submitting any review text, scan for both banned lists. If a banned word appears without a concrete translation in the same sentence, rewrite the sentence or delete it. This check is not optional.

## Step 1: Read the evaluation and fix summary

Read `EVALUATION.md` in this worktree. It contains up to three parts:
1. **Findings** — the numbered style violations identified by `/style_eval`
2. **Review Log** — appended by `/style_eval_review`, documenting which findings the review pass kept, improved, amended, or removed, and why
3. **Fix Summary** — appended by the fix agent, documenting what it did, what it skipped, and why

Start by reading the Fix Summary section at the bottom. This gives you the agent's own account of what happened — which findings were applied, which were skipped or partially applied, and any issues encountered (build failures, pattern mismatches, style conflicts, etc.). Also look for the **Cargo Mend Changes** subsection, which documents any automatic visibility/import fixes made by `cargo mend --fix`. Use this as your starting point: you'll verify these claims against the actual diff.

**Removed-by-review findings are reporting-only.** Findings whose body is wrapped in `<!-- REMOVED-BY-REVIEW: ... -->` ... `<!-- /REMOVED-BY-REVIEW -->` markers were struck by the review pass before the fix agent ran. The fix agent did not act on them and you should not evaluate the diff against them. Surface them in the Review Log section of your output (Step 5.5) so the user can see what was cut, but do not produce a finding walkthrough for them in Step 6.

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

## Step 5.5: Surface the Review Log

If `EVALUATION.md` contains a `## Review Log` section, summarize it under a `## Review Log` heading in your output. The user has not read it. Format:

- One line stating totals: `N findings reviewed: K kept, I improved, A amended, R removed.`
- If anything was improved, amended, or removed, render a short table:

  | # | Action | Reason |
  |---|---|---|
  | 1 | improved | tightened Recommended pattern wording |
  | 3 | removed | rule cited does not exist in the loaded style file |

Only include rows for non-`kept` actions. If every finding was kept, write one sentence: `Review pass kept all N findings as written.` and skip the table.

If `EVALUATION.md` has no `## Review Log` section, omit this section entirely — the eval predates the review stage or the review failed.

## Step 6: Review each finding

For each numbered finding in EVALUATION.md that is **not** wrapped in `<!-- REMOVED-BY-REVIEW -->` markers, assess:

- **What was done** — Summarize the actual changes made for this finding (files touched, what was moved/renamed/rewritten)
- **Applied?** — Was the finding addressed in the diff?
- **Correct?** — Does the change match the recommended pattern and conform to the style guide? Is the transformation accurate?
- **Complete?** — Were all entries in the finding's "Locations" list handled, or were some missed?
- **Side effects?** — Did the change introduce any bugs, break any patterns, or change behavior? When verifying renames or visibility narrowing, prefer LSP `findReferences` to ripgrep — references through type aliases, re-exports, or generic dispatch are invisible to text search. If LSP is unavailable, expand the ripgrep scope and note the limitation.
- **New allows?** — Did addressing this finding introduce any new allow that should be surfaced to the user?

## Step 7: Output order

The first response branches based on whether the Allow Audit is clean.

**Case A — Allow Audit is clean (`No new allows in the diff.`):**

1. Summary table with columns: `# | Finding | Applied | Correct | Complete | Issues` — list only findings NOT wrapped in REMOVED-BY-REVIEW markers; the row number is the finding's original number
2. `## Review Log` (per Step 5.5 format — omit if EVALUATION.md has no Review Log section)
3. `## Allow Audit` (per Step 4 format — one sentence)
4. `## Cargo Mend Changes` (per Step 5 format)
5. `## Finding N` walkthrough — N is the lowest-numbered finding NOT removed-by-review (per Step 8 format)

Then **stop**. Do not output Finding 2 or anything else in the same response. Wait for the user to reply before producing Finding 2 — and then Finding 2 only.

**Case B — Allow Audit flagged one or more non-pre-authorized allows:**

1. Summary table with columns: `# | Finding | Applied | Correct | Complete | Issues` — list only findings NOT wrapped in REMOVED-BY-REVIEW markers
2. `## Review Log` (per Step 5.5 format — omit if EVALUATION.md has no Review Log section)
3. `## Allow Audit` (per Step 4 format — itemized list)
4. `## Recommendations` (one concrete proposed action per flagged allow)
5. A direct question asking the user how to proceed.

Then **stop**. Do **not** output Cargo Mend Changes or Finding 1 in this response. Once the user has resolved the allow question in a subsequent turn, the next response outputs:

1. `## Cargo Mend Changes` (per Step 5 format)
2. `## Finding N` walkthrough — N is the lowest-numbered finding NOT removed-by-review (per Step 8 format)

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
5. **Implications** — bullet list only when the fix has a downstream consequence the user should weigh, even if no immediate action is required. An implication is something that may shape a *decision* the user makes next: a tradeoff, a precedent set, a knock-on effect on callers, a constraint added to future work. The user reading the bullet should be able to think "ah, that's a thing I now have to consider" — not "yes, the rule was followed." High bar: if it is just restating that a rule was satisfied, or if a competent reader would predict the consequence from the change itself (e.g. "narrower visibility means widening later if scope grows"), drop it. Omit the section entirely when empty. Never use this section as a softer home for passed checks.
6. **Concerns** — bullet list only if there are items that need the user's attention. Use the terse, scannable format below — not dense prose. If there are no real concerns and no new allows, omit the section entirely.

   **Required format per concern — copy this template exactly:**

   ```
   - **`<file:line or short identifier>`**

     **Issue:** <one short sentence naming what's wrong, with inline rule citation, e.g. "(rule: pixel-units-in-names.md)">.

     **Recommend:** <single recommended action, one short sentence>.
   ```

   The bullet has three parts, each on its own line and **separated by a blank line**: **headline** (file path in backticks), **`Issue:`** sub-section (one short sentence naming what's wrong, with an inline rule citation), **`Recommend:`** sub-section (a single recommended action). Do not run the parts together into one paragraph — the blank lines between them are mandatory and are what makes the bullet scannable.

   The bullet is **not** a proposal asking the user to choose between options. It is a clear statement of the defect plus a single recommendation. The user can override the recommendation in their reply if they disagree — but the bullet itself does not enumerate alternatives.

   **Hard limits:**
   - Each sub-section (`Issue:` and `Recommend:`) is one short sentence. If you need more, cut history and citation chains; keep the verdict.
   - **Blank lines between the headline, `Issue:`, and `Recommend:` are mandatory.** Running them together into one paragraph is a format violation, even if the content is otherwise correct.
   - No multi-clause sentences chained with em-dashes or "but". Each sub-section is one statement.
   - **The `Recommend:` sub-section is mandatory and must be the last sub-section.** It is a single declarative action, not a question. Do not list multiple options; pick one.
   - **No question-form action prompts.** Bullets ending in "Rename, leave for follow-up, or accept?" or "Reorder?" or "Revert, keep wrapper, or amend the guide?" are the old format and are now banned. Replace them with `**Recommend:** rename to <name>.` or similar.
   - No "Decision needed:" preamble. The `Recommend:` sub-section itself signals what action is suggested.
   - **No `**Style rule:**` or `**No rule found:**` paragraph preamble.** The lookup happens before you write the bullet (see the "do the style-rule lookup" hard rule above); the citation appears inline inside the `Issue:` sub-section as `(rule: <file.md>)` or `(no rule found)` only — never as a leading bold block that competes with the headline.

   **Good — canonical example. Copy this rhythm:**

   > - **`animation_poc_lerp.rs`**
   >
   >   **Issue:** agent moved `const CURSOR_NAME` inside `fn setup`; example targets get constants at top-of-file after imports (rule: `no-magic-values.md`).
   >
   >   **Recommend:** move the const back to file scope after the imports.

   **Good — second example with a missed-rename concern:**

   > - **`src/restore/winit_info.rs:95`**
   >
   >   **Issue:** `let monitor_position = current_monitor.position();` is a pixel-valued local that the eval missed and the agent did not rename (rule: `pixel-units-in-names.md`).
   >
   >   **Recommend:** rename to `physical_monitor_position` as a follow-up to this finding.

   **Bad — delete on sight (paragraph-shaped, no sub-section breaks):**

   > - **`src/restore/winit_info.rs:95`** — `let monitor_position = current_monitor.position();` is a same-kind pixel-valued local from the same `current_monitor.position()` call the eval flagged in `debug.rs:39`, but the eval's Locations list did not name it and the agent did not rename it (rule: `pixel-units-in-names.md`). **Recommend:** rename to `physical_monitor_position` as a follow-up to this finding.

   The above is wrong because the headline, issue, and recommendation are run together on one line as an em-dash-joined paragraph. Even though it ends in `**Recommend:**`, the lack of blank-line breaks between sub-sections makes it unscannable. Split into the three-part block shown in the Good examples.

   **Bad — delete on sight (multi-option question form; old format):**

   > - **`src/restore/winit_info.rs:95`** — `let monitor_position = current_monitor.position();` is a pixel-valued local the agent did not rename (rule: `pixel-units-in-names.md`). Rename to `physical_monitor_position`, leave for follow-up, or accept?

   The above follows the old "propose options" format. The new format requires a single `**Recommend:**` sub-section instead — let the user override in reply if they disagree.

   **Bad — delete on sight (`**Style rule:**` preamble; format violation):**

   > - **`src/restore/winit_info.rs:95`** — agent did not rename `let monitor_position = current_monitor.position();`. **Style rule (pixel-units-in-names.md):** *"every field whose value is a pixel count — position, width, height, size — must carry an explicit `logical_` or `physical_` prefix."* The exception covers locals where the qualifier is already present, which `monitor_position` is not. **Recommend:** rename to `physical_monitor_position`.

   The lookup happens internally; the bullet output uses the inline `(rule: <file>)` form inside the `Issue:` sub-section.

   **Pre-submit checklist for every Concerns bullet:**
   1. Does the bullet start with `**`<file>`**` on its own line — and nothing else on that line?
   2. Are the headline, `**Issue:**` line, and `**Recommend:**` line **separated by blank lines**? (If they are on one line joined by em-dashes, the format is wrong — split them.)
   3. Is there any `**Style rule` or `**No rule found` block-bold preamble? If yes, delete it; rewrite the citation as inline `(rule: <file.md>)` inside the `Issue:` sub-section.
   4. Is the rule citation a single short parenthetical, not a quoted prescription?
   5. Does the bullet end with a `**Recommend:** <single action>.` sub-section — not a question, not a list of options?
   6. If you wrote a question mark or comma-separated options at the end, rewrite as a single recommendation. The user can override in reply.
   7. Read each sub-section aloud — does either sub-section take more than ~10 seconds? If yes, cut.

   **Special cases (still follow the three-part block format):**
   - Newly added `#[allow(...)]`, `#![allow(...)]`, or `Cargo.toml` `"allow"`: name the lint and `file:line` in the headline; one short `**Issue:**` sub-section; one `**Recommend:**` sub-section.
   - Speculative concerns: prefix the `Issue:` sub-section with `(speculative)`.

   **Do not list passed checks here.** Silence is the signal that something was done well. Bullets ending in "OK", "good fit", "correct" — delete.

### Voice and audience for Implications, Concerns, and Assessment

The reader is a working engineer who has read EVALUATION.md once, has not re-read the style guide today, and is not deep in the diff right now. Write for that reader.

- **Plain language over guide-jargon.** Banned-vocabulary list and rewrite rule are in the "Hard rule: banned vocabulary in Concerns, Implications, and Assessment" section above. Apply it before submitting.
- **Lead with what the user can do or decide.** Concerns must be actionable: name the file:line, what's wrong, and the proposed fix. Implications must inform a *decision* the user might make next — a tradeoff, a precedent, a constraint on future work, a knock-on effect on callers. If a bullet does not change behavior or shape a decision, drop it.
- **One-pass readability test.** Before submitting any bullet, ask: would the reader, on first read, know what was changed and why this bullet is in front of them? If they would have to re-read the style guide or scroll back to the diff to parse it, rewrite it.
- **Never use the rule's name as the answer.** "The style file allows this" or "the rule's fallback applies" is not informative. State what was specifically done and what it costs or implies — concretely, in this codebase's vocabulary.
- **Format check before submitting.** Read each Concerns bullet aloud. If it takes more than ~10 seconds to say, or if the bullet does not end with a single `**Recommend:**` clause (no question mark, no comma-separated options), rewrite it. The Concerns bullet format is enforced — long-form prose and multi-option questions are bugs.
- **Anti-patterns to delete on sight:**
  - Bullets that end in an affirmation ("OK," "good fit," "correct," "this is fine"). These are passed checks; silence is the signal.
    - Negative example — delete on sight: *"Public-facade visibility unchanged. Items retain their original `pub(crate)` visibility... `pub(crate)` is the correct choice... No change needed — flagging only because Finding 1's restructuring made me check every visibility decision in the diff."* This is a passed check the reviewer self-flagged as non-actionable ("No change needed — flagging only because..."). Omit the bullet — and if it was the only bullet, omit the entire Concerns section.
  - Bullets that label the kind of module/type/pattern without saying what follows from that label.
  - Speculative future-proofing ("if X happens later, watch out") without a current trigger in the diff. If it is genuinely worth flagging, tie it to something the user is deciding *now*.

Apply this voice to **Assessment** as well — keep it to a sentence a non-immersed reader can parse on first pass.

Then **stop the response**. Do not preview the next finding. Do not list upcoming findings. Do not write an end-of-review summary.

The user may have corrections, questions, or want you to fix something before proceeding. If a finding is incomplete, incorrect, or otherwise needs follow-up changes, explicitly ask whether the user wants to fix it now or wait until the end of the review before continuing.

When the user's next message arrives, produce exactly one finding walkthrough — the next numbered one — and stop again. Repeat until the user has seen every finding individually.

**On the last finding** — see the "Hard rule: the last finding stops like every other finding" section above. The walkthrough still ends with stop; the only addition is a single closing question asking whether the user wants the end-of-review summary. Do **not** produce the summary in that turn. Wait for the user to ask.

**Rules:**
- Do NOT make any changes — this is a read-only review
- Do NOT commit anything
- If the diff is empty (automation produced no changes), say so and note which findings were skipped
