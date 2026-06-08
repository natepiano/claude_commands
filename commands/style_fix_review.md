---
description: Review changes in a style-fix worktree against its clean-fix evaluation findings
---

**Context:** You are in a `_style_fix` worktree created by the clean-fix automation. The clean-fix pipeline:
1. Ran `/style_eval` on the main branch, storing numbered findings in pending JSON
2. Created this worktree on branch `refactor/style`
3. Launched Claude to apply every finding, run clippy, and run tests
4. Left the worktree with uncommitted changes for human review

**Your task:** Review the changes the automation made and determine whether each finding was correctly and completely addressed.

<Audience>
The user sees only what you write. They have **not** read the pending evaluation's Fix Summary, the `cargo mend` output, the diff, or any style file. Your job is to state what they *don't* know — without padding out what they already do know.

**Trust the reader on common ground.** Assume the user knows:
- Standard Rust and Cargo tooling (`cargo`, `clippy`, `cargo mend`, `rustfmt`, `nextest`)
- Common clippy lint names (`redundant_closure`, `single_match`, etc.)
- Basic Rust concepts (traits, visibility modifiers, closures, attributes)
- The repo layout and its own style guide

Do **not** re-introduce these. No "cargo mend is a workspace auditor..." preamble, no explaining what `#[allow]` does.

**The user does not know** (and you must state explicitly):
- What the Fix Summary actually says — quote or paraphrase, don't reference by name
- Project-specific lints, configs, or files (`forbidden_pub_crate`, `tests/support.rs` helpers) the first time they appear
- Cross-tool interactions (e.g. mend's fix being reverted by another lint)
- Any claim of a "known issue" or "expected limitation"

**Formatting rules:**
- One-sentence answers when one sentence is enough. "No new allows in the diff." is a complete Allow Audit.
- Use **tables** for repetitive mechanical changes (multiple files with the same before/after) — before/after columns beat bullet-point prose every time.
- For a multi-step mechanism (lint A fights lint B, etc.), use: one intro sentence + numbered steps + a one-line recommendation. Not paragraphs.
- Don't forward-reference findings by number before the reader has read them. If a mend change is tied to a later finding, describe the mechanical change without naming the finding — the connection surfaces naturally when they reach it.
- **Show evidence before interpretation** when the claim is non-obvious. If it's obvious (e.g. "trait removed, import rewritten to match"), skip the evidence preamble.
</Audience>

<HardRules>

### Never apply without explicit approval

After proposing a fix or change, stop. Do not run Edit, Write, Bash (for mutation), git operations, or any tool that changes the repository until the user has replied with an explicit go-ahead ("approve", "apply", "yes", "do it", etc.).

This applies to **every** phase of this skill — initial finding walkthrough and any follow-up task list. There is no phase in which self-approval is acceptable.

- Asking follow-up questions is fine; applying based on my own answers to those questions is not.
- `auto mode`, `/auto`, and similar global runtime flags do not override this rule. The per-task "propose → wait → apply → stop" rhythm comes from explicit user instruction and is the stronger signal.
- Read-only tools (Read, Glob, Grep, Bash for inspection with no write side-effects) are allowed between proposal and approval — they sharpen the proposal, not execute it.
- A user response that is a clarification or question (not an approval) restarts the cycle: refine the proposal and stop again.
- If you catch yourself starting to reach for Edit/Write before explicit approval, stop the chain, do not submit the tool call, and re-state the proposal.

### One finding per response

This skill is a strictly sequential walkthrough. After the initial header response (summary table + Allow Audit + Cargo Mend Changes, then Finding 1 **only if the Allow Audit is clean** — see "Allow Audit halt" below), every subsequent response contains **exactly one** finding walkthrough and then stops.

- When the user replies with *any* continuation signal ("continue", "next", "ok", "go", "yes", "keep going", etc.), advance by exactly one finding. Never two. Never "the rest."
- Do not output an end-of-review summary or queued-task list until the user has seen and responded to each individual finding.
- If the remaining findings look mechanical, trivial, or redundant — still output them one at a time. The user is pacing the review, not you.
- This rule overrides any instinct toward "efficiency" or "wrapping up."

### The last finding stops like every other finding

The final finding is **not special**. Same treatment as every other: walkthrough, then stop. No end-of-review summary, no recap table, no "all 5 findings have been walked," no follow-up task list, no "want me to do anything before you commit?" — *nothing* in the same response as Finding N's walkthrough.

The model's instinct will be to "wrap up" because there are no more findings. Resist it. Appending a summary in the same response as the last finding scrolls the last finding off-screen and dumps cognitive load on top of fresh content.

After the last finding's walkthrough, end with **one short closing line** that explicitly asks whether the user wants the end-of-review summary:

> *"That's the last finding. Want the end-of-review summary, or are we done?"*

Then stop. The summary appears only in the *next* response, and only if the user asks for it.

This rule applies regardless of whether `auto mode` is active, whether previous findings looked similar, whether the user said "continue" with no further instruction, whether the changes were trivial, or whether you feel the wrap-up would be brief.

### Read the governing style file before every fix proposal

Every fix proposal must be preceded by reading the governing style file and quoting its prescription. Do not brainstorm options when the style guide has already ruled.

- **Follow-up tasks derived from a finding** — the governing file is the finding's `Style file:` field in the pending evaluation markdown. Read it; cite it.
- **Follow-up tasks tied to a clippy lint** — the governing file is whichever style doc has the lint name in its frontmatter `lint:` field. Find it by grepping the loaded style files for the lint name.
- **Every proposal must start with a `**Style rule:**` line** quoting the relevant prescription, or stating "no specific rule; proposing by analogy to X."

If you find yourself listing options (a)/(b)/(c) before citing the rule, stop and do the lookup first. The rule often names the answer.

**Secondary concerns within a fix** — a single fix may touch concerns the finding's style file does not cover (e.g. a module split that incidentally needs to move an `#[allow]`, or an API change that raises a naming question). Treat each as its own lookup:

1. **Identify every independent style concern the fix involves** before drafting the proposal — module boundaries, lint suppressions, naming, visibility, comments, observers, error handling — each typically governed by a separate style file.
2. **For each concern, find the governing file:** if it's a clippy lint, grep for the lint name in loaded style files; if it's a pattern (visibility, module layout, observers, etc.), grep for the domain keywords or scan filenames; if nothing matches, state that explicitly: "no style rule found for <concern>; proposing <choice> because <reasoning>."
3. **Cite each governing file** with a `**Style rule (<concern>):**` line. A fix touching three concerns should cite three rules (or explicitly note which have no governing rule).
4. **Never propose a pattern for a secondary concern without doing the lookup**, even if the concern feels minor.

### Do the style-rule lookup before every Concern entry

This is the review-time mirror of the rule above. It applies to every Concern entry, including ones about details the finding itself did not raise (visibility, imports, mod declarations, naming, allows, observers, error handling, etc.).

**The lookup is mandatory.** Before writing any Concern, you must have one of:

- Found the governing style file and identified the relevant prescription, or
- Searched and found nothing applicable (record which files you grepped and which lint names you looked up).

If you cannot do one of those after a real lookup, delete the entry. "It feels off" is not a Concern; it is a prompt to do the lookup or drop the worry.

**How the lookup shows up in output:** as a single short inline tag, not a leading preamble. See `<ConcernFormat/>` for the template — typically `(rule: <file.md>)` or `(no rule found)` placed inside the `Issue:` sentence as a parenthetical. Never as a `**Style rule:**` block.

**Visibility-trigger checklist.** If the diff contains **any** of:

- a new or changed `pub(...)` modifier
- a new `mod` declaration (whether `pub`, `pub(super)`, `pub(crate)`, or private)
- a new or changed `pub use` / `pub(super) use` / `pub(crate) use` re-export
- a new leaf file under an existing module directory

…you must read these files before writing any Concern that touches visibility, module structure, or import paths:

- `~/rust/nate_style/rust/leaf-module-visibility.md`
- `~/rust/nate_style/rust/no-pub-in-path.md`
- `~/rust/nate_style/rust/no-pubcrate-in-nested-modules.md`

Read them even when the finding's own `Style file:` points elsewhere. The mend loader includes these files (they are `mode: flag`, not `mode: auto`), but inclusion in the loader does not guarantee you consulted them.

### Allow Audit halt

If the Allow Audit surfaces **any** new allow that is not pre-authorized by the style guide, the skill **must halt after the Allow Audit section** — before Cargo Mend Changes, before Finding 1. New allows are a direct violation of `agent-must-review-allows.md` and must be resolved with the user before the rest of the review proceeds.

- After the Allow Audit, add a `## Recommendations` section listing each flagged allow with one concrete proposed action (remove, restructure, relocate to `mod` declaration, etc.).
- End the response with a direct question asking the user how to handle the flagged allow(s) before continuing the review.
- Do **not** output Cargo Mend Changes or Finding 1 in the same response.
- Only proceed in the next response, after the user has directed how to resolve (or accept) each flagged allow.
- If the Allow Audit is `No new allows in the diff.`, this halt does not apply — continue normally.

### Banned vocabulary in review output

Applies to **every section the user reads** — Original issue, What changed, Assessment, Implications, Concerns, and any narration around the summary table.

**Source of truth — the shared forbidden-words guide.** All project-wide banned stems (with their forms, substitutes, and counters) live in the shared file below. Read it and apply every rule it states. The hook at `~/.claude/scripts/hooks/post-tool-use-banned-words.py` enforces this list on every Write/Edit, so a banned stem leaking into review output will block the response. The shared guide is the single source of truth — when it changes, this command picks up the change with no edit here.

@~/rust/nate_style/rust/forbidden-words.md

**Review-context additions** (specific to /style_fix_review output, not in the shared guide):

- **Guide-jargon** (opaque to a reader who hasn't reread the docs today): `domain cohort`, `domain-cohort`, `domain-noun cohort`, `behavior owner`, `dictionary file`, `data dictionary`, `junk drawer`, `cohort name`. Translate each into concrete codebase nouns (file names, type names, function names) and a thing the user can do or decide. <!-- allow-banned: enumerates review-context banned terms -->
- **Decorative metaphor verbs and adjectives** beyond the shared list: `sculpted`, `woven`, `threaded`, `surfaced` (as a transitive verb meaning "exposed"), `crystallized`, `distilled`, `pipeline-shaped`, `module-shaped`, anything-`-shaped`, anything-`-flavored`. Replace with the literal verb (`split`, `grouped`, `organized`, `divided`, `extracted`, `moved into`). <!-- allow-banned: enumerates review-context banned terms -->

**Canonical delete-on-sight examples:**

1. *"`controller.rs` is a domain-noun cohort name rather than an anchor type name."*
   `domain-noun cohort` is pure guide-jargon, nothing actionable. Either drop the entry, or rewrite concretely: *"`controller.rs` holds the `orbit_cam` runtime system plus its private input/transform helpers — one weight-bearing item, not several peers. A name like `runtime.rs` or `pipeline.rs` would predict the contents better."* <!-- allow-banned: orbit_cam is an example Rust identifier -->

2. *"The restore subtree was carved by pipeline phase (`apply/bootstrap.rs`, `apply/cross_dpi.rs`, …)."* <!-- allow-banned: example of banned text being illustrated -->
   `carved` is a decorative verb that says nothing the file list doesn't already say. Rewrite: *"The restore subtree is split into one file per pipeline phase: `apply/bootstrap.rs`, `apply/cross_dpi.rs`, …"* <!-- allow-banned: explains the prior example -->

Before submitting, scan output against both the shared guide above and the review-context additions. If a banned word appears without a concrete translation in the same sentence, rewrite or delete. Not optional.

### Voice for Implications, Concerns, and Assessment

The reader is a working engineer who has read the evaluation once, has not re-read the style guide today, and is not deep in the diff right now.

- **Plain language over guide-jargon.** Apply the banned-vocabulary rule above before submitting.
- **Lead with what the user can do or decide.** Concerns must be actionable: file:line, what's wrong, proposed fix. Implications must inform a *decision* the user might make next — a tradeoff, a precedent, a constraint on future work, a knock-on effect. If a bullet does not change behavior or drive a decision, drop it.
- **One-pass readability test.** Would the reader, on first read, know what was changed and why this entry is in front of them? If they would have to re-read the style guide or scroll back to the diff to parse it, rewrite.
- **Never use the rule's name as the answer.** "The style file allows this" or "the rule's fallback applies" is not informative. State what was specifically done and what it costs or implies — concretely, in this codebase's vocabulary.
- **Anti-patterns to delete on sight:**
  - Entries that end in an affirmation ("OK," "good fit," "correct," "this is fine"). Passed checks; silence is the signal.
  - Entries that label the kind of module/type/pattern without saying what follows from that label.
  - Speculative future-proofing ("if X happens later, watch out") without a current trigger in the diff.

</HardRules>

<ConcernFormat>

**Template — copy this literally. The headline is the only numbered line. The Issue and Recommend lines are bullets (`- `) nested under the headline.**

```
1. **`<file:line>`**
   - **Issue:** <one short sentence>. (rule: `<file.md>`)
   - **Recommend:** <one short imperative sentence>.
```

The headline is a numbered list item with the file path in backticks and bolded. `**Issue:**` and `**Recommend:**` are bullets nested under the headline — each line starts with exactly 3 spaces, then `- `, then the bold label. They are **not** numbered. No blank line between the three lines of one concern; one blank line between concerns.

**Why bullets, not continuation paragraphs.** Earlier versions used 3-space-indented continuation paragraphs separated by blank lines. That pattern reliably drifted into sibling numbered items (`1. Issue:` / `1. Recommend:`) which renderers then auto-renumbered into a flat list, destroying the grouping. Bullets nested under a numbered item are visually unambiguous and survive every renderer.

If the lookup turned up no governing rule, write `(no rule found)` instead of the citation.

**Numbering.** Restart at `1.` for each finding. Even a single concern is `1.`.

**Silence is the signal.** A Concern must propose a concrete change to the diff: rename, rewrite, move, delete, add a test, remove an allow. If the recommendation would be "leave as-is," "accept," "flagging only," or any equivalent, **do not emit the entry.** Drop it. If the finding has no actionable concerns, omit the Concerns section entirely. Passed checks belong in silence, not in the output.

**Example:**

> 1. **`animation_poc_lerp.rs`**
>    - **Issue:** agent moved `const CURSOR_NAME` inside `fn setup`; example targets get constants at top-of-file after imports. (rule: `no-magic-values.md`)
>    - **Recommend:** move the const back to file scope after the imports.

**Common mistakes — re-read your draft for these:**

- Numbered `Issue:` or `Recommend:` lines (`1. Issue:`, `2. Recommend:`). Only the headline is numbered. Sub-lines are `- ` bullets.
- Issue/Recommend emitted as plain indented text or as their own numbered list items instead of `- ` bullets nested under the headline.
- Bare file path in the headline without backticks or bold.
- Wrong indent on the bullets — use exactly 3 spaces before `- ` so the bullet nests under the numbered headline. Two spaces or four will render as a sibling list, not a child.
- `**Style rule:**` or `**No rule found:**` paragraph preamble. The citation goes inline as `(rule: <file.md>)` inside `Issue:`.
- `Recommend:` phrased as a question or multi-option list ("rename, leave for follow-up, or accept?"). Single imperative sentence.
- Concern that recommends no action. Drop the entire entry.

**Pre-send scan — mandatory before emitting any Concerns block.** Run these checks against your draft and rewrite if any match:

1. Any line matching `^\s*\d+\.\s+(Issue|Recommend):` — that is the auto-renumber failure mode. Rewrite as `   - **Issue:** ...` / `   - **Recommend:** ...`.
2. Any headline line not starting with `N. **\``. Add the backticks-and-bold wrapper.
3. Any Issue/Recommend line that does not begin with `   - **`. Re-anchor to the bullet form.
4. Any Concern entry whose Recommend would be "leave as-is" or equivalent. Delete the entry.

If the draft fails any of these, do not send — fix and re-scan.

**Special cases (still follow the template):**

- Newly added `#[allow(...)]`, `#![allow(...)]`, or `Cargo.toml` `"allow"` entries: name the lint and `file:line` in the headline.
- Speculative concerns: prefix `Issue:` with `(speculative)`. Still must end in an actionable `Recommend:` — if no concrete action, drop it.

**Do not list passed checks here.** Silence is the signal that something was done well. Entries ending in "OK", "good fit", "correct" — delete.

</ConcernFormat>

<ReadEvaluation>
**Load the style-fix evaluation from pending JSON.** Clean-fix no longer writes `EVALUATION.md` into style-fix worktrees. The durable evaluation markdown lives in `~/rust/nate_style/.history/.pending/<project>.json` under `evaluation_markdown`. Scratch files under `/private/tmp/claude` are exports only; their absence is not a reason to stop.

Run this from anywhere inside the style-fix worktree to derive the project name, inspect the pending state, and export the pending markdown to a temporary review file:

```bash
worktree_dir="$(git rev-parse --show-toplevel)"
project="$(basename "$worktree_dir")"
project="${project%_style_fix}"
status_json="$(python3 ~/.claude/scripts/clean-fix/style_history.py evaluation-status --project "$project")"
eval_path="/private/tmp/claude/style_fix_review_${project}_evaluation.md"
python3 ~/.claude/scripts/clean-fix/style_history.py export-evaluation \
  --project "$project" \
  --kind review \
  --output "$eval_path"
printf 'project=%s\neval_path=%s\nstatus=%s\n' "$project" "$eval_path" "$status_json"
```

If `export-evaluation` fails with `No pending evaluation markdown for <project>.`, stop and report that the pending evaluation markdown is missing. Do **not** search for repo-root `EVALUATION.md`; those files are stale artifacts and must not be used as review input.

Read the exported pending markdown. It contains up to three parts:

1. **Findings** — numbered style violations identified by `/style_eval`
2. **Review Log** — appended by the clean-fix style-eval-review stage, documenting which findings the review pass kept, improved, amended, or removed, and why
3. **Fix Summary** — appended by the fix agent, documenting what it did, what it skipped, and why

If the exported pending markdown has no `## Fix Summary`, parse `status_json` and check `scratch_exports.fix.path`. If that path exists and contains `## Fix Summary`, read that file only for Fix Summary and Cargo Mend Changes; keep the exported pending markdown as the authority for findings and Review Log. If neither pending markdown nor the recorded fix export contains `## Fix Summary`, continue the review from the pending findings and actual diff, and state once in `## Cargo Mend Changes`: `Fix Summary unavailable; reviewing the diff directly against pending findings.`

Also read the header fields. If `**Rules checked**:` is in the new coverage
format (`N/M (stop_reason)`), include that coverage in your summary table
context so the user can tell whether the eval exhausted the selected style
units or stopped at the finding budget. If it is a legacy bare number with no
denominator/stop reason, say that explicitly; do not imply a full style-guide
sweep.

If a Fix Summary is available, start by reading it. This is the agent's account of what happened — applied / skipped / partially applied, plus issues encountered (build failures, pattern mismatches, style conflicts, etc.). Also look for **Cargo Mend Changes**, which documents automatic visibility/import fixes made by `cargo mend --fix`. Use this as your starting point: you'll verify these claims against the actual diff.

**Removed-by-review findings are reporting-only.** Findings whose body is wrapped in `<!-- REMOVED-BY-REVIEW: ... -->` ... `<!-- /REMOVED-BY-REVIEW -->` markers were struck by the review pass before the fix agent ran. The fix agent did not act on them and you should not evaluate the diff against them. Surface them in the Review Log section so the user can see what was cut, but do not produce a finding walkthrough for them.
</ReadEvaluation>

<LoadStyleGuide>
Run:

```bash
zsh ~/.claude/scripts/load-rust-style.sh
```

Then read each unique style file referenced by the findings. Each finding in the pending evaluation markdown includes a **Style file** field with the full path (e.g., `~/rust/nate_style/rust/one-use-per-line.md` or a repo-local `docs/style/*.md`).

The referenced files are your authoritative sources for evaluating whether the changes conform.

**Follow `see_also` cross-references.** When a style file's frontmatter has a `see_also:` field (a wikilink like `"[[other-file]]"` or a list), read those files too and treat them as additional context. The loader output already appends see_also content under a `### Related style guidance (via see_also → ...)` subheading. Do not raise the see_also'd rule as a separate concern — it informs how you interpret the primary rule.
</LoadStyleGuide>

<ReadDiff>
Run `git diff` to see all unstaged changes. If there are also staged changes, run `git diff --cached` as well.
</ReadDiff>

<AuditAllows>
Inspect the diff for newly added suppressions: `#[allow(...)]`, `#![allow(...)]`, or lint-table entries set to `"allow"` in `Cargo.toml` or workspace lints. A `reason = ...` does not exempt an allow from being reported.

**Output format:**

- If none found: one sentence — `No new allows in the diff.` Done.
- If any found: a short list, one per allow, with: `file:line` — lint name — `reason` (or `(none)`) — whether the style guide pre-authorizes it — a one-phrase note on whether it looks avoidable.

Consult `agent-must-review-allows.md`, `never-bare-allowdeadcode.md`, `cargo-toml-lints.md`, `cargo-toml-bevy-lints.md`, and any finding-specific style file to judge pre-authorization. Do not explain the methodology in output — just report.

**If any non-pre-authorized allow was found, halt** per the Allow Audit halt rule:

1. Append a `## Recommendations` section. For each flagged allow, give one concrete proposed action (e.g. "remove and re-run clippy to see if still needed", "move to `#[allow(...)] mod foo;` on the parent `mod` line per `used-underscore-binding-module-level-allow-only.md`", "restructure the call site to eliminate the lint").
2. End with a direct question asking how to handle the flagged allow(s).
3. Do **not** output Cargo Mend Changes or Finding 1 in the same response.
</AuditAllows>

<ReviewCargoMend>
Check the Fix Summary for a **Cargo Mend Changes** section. Do **not** introduce what `cargo mend` is.

**If no Fix Summary is available:** write exactly `Fix Summary unavailable; reviewing the diff directly against pending findings.` and stop this section.

**If mend-credited edits exist in the diff:**

Summarize as a before/after table. Group files that share the same before/after into a single row. Do **not** cross-reference numbered findings here — the reader hasn't read them yet.

| file(s) | before | after |
|---------|--------|-------|
| ... | ... | ... |

Follow with at most one sentence classifying the overall pattern (e.g. "All six are import-path rewrites; none narrowed visibility.").

**If the Fix Summary reports an unfixable or cycling mend item:**

Use this format — intro sentence, numbered sequence, one-line recommendation:

> **`<file>` cycles:** `<one-sentence description>`.
> 1. `<step one>`
> 2. `<step two>`
> 3. `<step three>`
>
> Agent left it as `<current state>`. Follow-up: `<single-line recommendation>`.

Name any project-specific lint the first time it appears (one clause is enough — e.g. "`forbidden_pub_crate` (repo-configured lint that rejects `pub(crate)` on test helpers)").

**If mend made no changes or was skipped:** one sentence. Done.
</ReviewCargoMend>

<SurfaceReviewLog>
If the pending evaluation markdown contains a `## Review Log` section, summarize it under a `## Review Log` heading in your output. The user has not read it. Format:

- One line stating totals: `N findings reviewed: K kept, I improved, A amended, R removed.`
- If anything was improved, amended, or removed, render a short table:

  | # | Action | Reason |
  |---|---|---|
  | 1 | improved | tightened Recommended pattern wording |
  | 3 | removed | rule cited does not exist in the loaded style file |

Only include rows for non-`kept` actions. If every finding was kept, write one sentence: `Review pass kept all N findings as written.` and skip the table.

If the pending evaluation markdown has no `## Review Log` section, omit this section entirely — the eval predates the review stage or the review failed.
</SurfaceReviewLog>

<ReviewFindings>
For each numbered finding in the pending evaluation markdown that is **not** wrapped in `<!-- REMOVED-BY-REVIEW -->` markers, assess:

- **What was done** — Summarize the actual changes (files touched, what was moved/renamed/rewritten)
- **Applied?** — Was the finding addressed in the diff?
- **Correct?** — Does the change match the recommended pattern and conform to the style guide?
- **Complete?** — Were all entries in the finding's "Locations" list handled, or were some missed?
- **Side effects?** — Did the change introduce bugs, break patterns, or change behavior? When verifying renames or visibility narrowing, prefer LSP `findReferences` to ripgrep — references through type aliases, re-exports, or generic dispatch are invisible to text search. If LSP is unavailable, expand the ripgrep scope and note the limitation.
- **New allows?** — Did addressing this finding introduce any new allow that should be surfaced?
</ReviewFindings>

<OutputHeader>
The first response branches based on whether the Allow Audit is clean.

**Case A — Allow Audit is clean (`No new allows in the diff.`):**

1. Summary table: `# | Finding | Applied | Correct | Complete | Issues` — list only findings NOT wrapped in REMOVED-BY-REVIEW markers; row number is the finding's original number
2. `## Review Log` (per `<SurfaceReviewLog/>` — omit if the pending evaluation markdown has no Review Log)
3. `## Allow Audit` (per `<AuditAllows/>` — one sentence)
4. `## Cargo Mend Changes` (per `<ReviewCargoMend/>`)
5. `## Finding N` walkthrough — N is the lowest-numbered finding NOT removed-by-review (per `<FindingWalkthrough/>`)

Then **stop**. Do not output Finding 2 or anything else.

**Case B — Allow Audit flagged one or more non-pre-authorized allows:**

1. Summary table (same columns as Case A)
2. `## Review Log` (omit if absent)
3. `## Allow Audit` (itemized list)
4. `## Recommendations` (one concrete proposed action per flagged allow)
5. A direct question asking how to proceed.

Then **stop**. Do **not** output Cargo Mend Changes or Finding 1 in this response. Once the user resolves the allow question in a subsequent turn, the next response outputs:

1. `## Cargo Mend Changes`
2. `## Finding N` walkthrough — N is the lowest-numbered finding NOT removed-by-review

— and then stops, following the one-finding-per-response rule.
</OutputHeader>

<FindingWalkthrough>
For each finding, output a compact block with these parts. Keep it tight — a paragraph or two plus short lists is usually enough.

1. **Style file path** — one line, from the finding's `Style file` field.
2. **Original issue** — one to two sentences naming the actual identifiers (traits, files, fields) that were flagged and what the evaluation asked for. Not "Finding 2 was about single-impl traits" — name them.
3. **What changed** — concrete before/after for the main edits.
   - **Always anchor edits to file paths.** Every "what changed" claim must cite the file it lives in. Non-negotiable.
   - **Format decision rule.** Use a table *only* when each before/after cell fits on one line of real code with no ellipses and no prose. If the change spans multiple lines or restructures, use a fenced code block with `Before:` / `After:` headers. If the change can't be shown as code (file moves, deletions, whole-file renames), use a prose line with the path.
   - **Anti-pattern.** If a before/after cell contains English describing what the code did (e.g. "returning `Option` via the match"), the format is wrong — promote to a code block. Cells hold code, not commentary.
   - **Tables** — include a `file` column as the first column. Files go in their own column, not embedded in before/after cells.

     | file | before | after |
     |---|---|---|
     | `src/fit.rs` | `is_ortho: bool` | `mode: ProjectionMode` |
   - **Code blocks** — use for multi-line or structural changes:

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
   - **Prose form** — use for file moves, splits, deletions, or renames of whole files. Pattern: `` `old/path.rs` → `new/path.rs` `` with a one-clause description. Never write "X was renamed to Y" without paths.
4. **Assessment** — a single line or short sentence: applied / correct / complete, and one phrase citing the style rule.
5. **Implications** — bullet list only when the fix has a downstream consequence the user should weigh, even if no immediate action is required. An implication drives a *decision* the user makes next: a tradeoff, a precedent, a knock-on effect on callers, a constraint on future work. The reader should think "ah, that's a thing I now have to consider" — not "yes, the rule was followed." High bar: if it just restates that a rule was satisfied, or if a competent reader would predict the consequence from the change itself, drop it. Omit the section entirely when empty. Never use this section as a softer home for passed checks.

   **Hard rule — no readability implications for clippy-driven rewrites.** When a change is attributable to a clippy lint (auto-fix in phase 5b, manual fix in 5c, or any rewrite citing a lint name in the Fix Summary), the user has already opted into clippy's preference by allowing the lint in `Cargo.toml`. Do **not** flag the rewritten form as harder to read, less direct, more verbose, or aesthetically worse than the pre-clippy form. Do **not** propose `#[allow(...)]` to revert it. Silence is the correct default. Examples to filter out: "the `mul_add` form reads less directly than `(a - b * c).abs()`," "the `unwrap_or` rewrite obscures the closure's intent," "the rewritten `&impl AsRef<str>` signature is less explicit than `&str`."

   **Exception — surface a clippy-driven change only if it has a non-readability consequence**:
   - **Behavioral or numerical divergence** — not bit-identical or changes observable semantics (iteration order, panic conditions, overflow behavior, NaN handling, evaluation order of side effects). <!-- allow-banned: 'bit-identical' is technical terminology -->
   - **API or visibility change** — alters a public signature, exported type, or visibility modifier callers depend on.
   - **Lint-vs-lint conflict** — the clippy fix re-introduces a violation of another lint or style rule.
   - **Performance regression in a hot path** — measurably slower in a code path that matters; flag only with concrete evidence.
   - **Constraint added to future work** — locks in a pattern that a planned refactor (named in project context) will need to undo.

   Pure readability is **never** on the list.

   **How to detect a clippy-driven change:**
   1. The Fix Summary's `Clippy Changes` subsection names the file and/or lint.
   2. The fix sequence is eval-driven edits → `cargo mend` → `cargo clippy --fix` → manual clippy. Anything appearing post-eval matching a known clippy rewrite pattern (`mul_add`, `unwrap_or_else` → `unwrap_or`, `&str` → `impl AsRef<str>`, `if let Some(_) =` → `is_some()`, etc.) is presumptively clippy-driven.
6. **Concerns** — numbered list only if there are items that need the user's attention. Use `<ConcernFormat/>` exactly. If there are no real concerns and no new allows, omit the section entirely.

   **Before sending the response, run the `<ConcernFormat/>` pre-send scan against every Concern entry in your draft.** This is mandatory, not optional. The four checks (auto-renumber, headline wrapper, bullet anchor, no-action drop) catch the failure modes that recur across reviews. Do not send until the draft passes all four.

After the walkthrough, **stop the response**. Do not preview the next finding. Do not list upcoming findings. Do not write an end-of-review summary.

The user may have corrections, questions, or want you to fix something before proceeding. If a finding is incomplete, incorrect, or otherwise needs follow-up, explicitly ask whether the user wants to fix it now or wait until the end of the review before continuing.

When the user's next message arrives, produce exactly one finding walkthrough — the next numbered one — and stop again. Repeat until the user has seen every finding individually.

**On the last finding** — see the "last finding stops like every other finding" rule in `<HardRules/>`. The walkthrough still ends with stop; the only addition is a single closing question asking whether the user wants the end-of-review summary. Do **not** produce the summary in that turn.
</FindingWalkthrough>

<Constraints>
- Do NOT make any changes — this is a read-only review.
- Do NOT commit anything.
- If the diff is empty (automation produced no changes), say so and note which findings were skipped.
</Constraints>

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER. Internalize `<HardRules/>`, `<Audience/>`, `<ConcernFormat/>`, and `<Constraints/>` before producing any output — they govern every step below.**

**STEP 1:** Execute `<ReadEvaluation/>` — start with the Fix Summary from pending markdown, or from the recorded fix export if pending does not include it.
**STEP 2:** Execute `<LoadStyleGuide/>` — load the global style guide and read each style file referenced by surviving findings.
**STEP 3:** Execute `<ReadDiff/>` — `git diff` and `git diff --cached`.
**STEP 4:** Execute `<AuditAllows/>` — inspect for new allow suppressions.

   → **If any non-pre-authorized allow was found:** produce the Case B header per `<OutputHeader/>` (summary table, Review Log, Allow Audit, Recommendations, question). **Stop.** Do not run STEP 5+ in this response. Resume at STEP 5 in the next response after the user resolves the allow question.

**STEP 5:** Execute `<ReviewCargoMend/>` — examine the Fix Summary's Cargo Mend Changes subsection and verify against the diff.
**STEP 6:** Execute `<SurfaceReviewLog/>` — summarize the Review Log section if present.
**STEP 7:** Execute `<ReviewFindings/>` — for each non-removed finding, gather the assessment data (what was done, applied/correct/complete, side effects, new allows).
**STEP 8:** Produce the Case A header per `<OutputHeader/>` (summary table, Review Log, Allow Audit, Cargo Mend Changes), then the **first** Finding walkthrough per `<FindingWalkthrough/>`. **Stop.**
**STEP 9:** On each subsequent user continuation signal, produce **exactly one** Finding walkthrough per `<FindingWalkthrough/>` and stop. Repeat until every non-removed finding has been walked.
**STEP 10:** After the last finding's walkthrough, end with the closing question — *"That's the last finding. Want the end-of-review summary, or are we done?"* — per the "last finding" hard rule. **Do not produce the summary in the same response.** If and only if the user asks for it in their next message, produce the end-of-review summary then.
</ExecutionSteps>
