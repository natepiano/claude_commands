---
description: Review the EVALUATION.md produced by /style_eval and improve, amend, or remove findings before /style_fix runs
---

**IMPORTANT**: This is a review of an existing `EVALUATION.md`. You may modify `EVALUATION.md` (improve, amend, or remove findings). You may NOT modify any source code. You may NOT modify any style guide file. You may NOT add new findings the eval did not surface — adding new findings is the job of `/style_eval`, not this pass.

## Arguments

- `$ARGUMENTS` is the absolute path to the project root (the directory containing the `EVALUATION.md` to review).

## Goal

`/style_eval` produces a list of style-guideline findings. Some are sharp; some are vague, wrong, or out of scope. Before `/style_fix` spawns a coding agent against the file, walk every finding once with the governing style guide in hand and:

- **Improve** — tighten wording, sharpen the rule citation, clarify the suggested change.
- **Amend** — narrow scope, correct misattributions, fix wrong file paths or line refs, drop locations that no longer match.
- **Remove** — strike findings that are wrong, already-followed, or out of scope.

Removed findings stay in the file (wrapped in a marker block) so the human reviewing the worktree can see what you cut and why. They are reporting-only; downstream agents must not act on them.

## Step 1: Read EVALUATION.md

Read `$ARGUMENTS/EVALUATION.md`. If the file does not exist, or contains no `### N.` numbered findings under `## Improvements`, write nothing and exit — there is nothing to review.

If the file already contains a `## Review Log` section, this evaluation has already been reviewed. Exit without changes.

## Step 2: Load every style file the findings cite

Each finding under `## Improvements` has a `**Style file**:` line with the full path to the governing style file. Collect the unique paths and read them all. These — plus any `see_also` files referenced by their frontmatter — are your authoritative rules. Do not consult any other style source. Do not load the full style guide.

If a cited style file does not exist on disk, treat that finding as a candidate for **removal** (the rule it claims to enforce is no longer authoritative).

## Step 3: Verify each finding against its governing style file and the cited code

For each numbered finding, in order:

1. Read the **Style file** for that finding. Re-read its frontmatter (`mode:`, `mechanism:`, `lint:`) and its prescription.
2. Open every path in the finding's **Locations** list and verify the cited site still exhibits the violation.
3. Decide one of four actions:
   - **Keep as-is** — the finding is accurate, well-scoped, and clearly written. No edit.
   - **Improve** — the finding is correct but the title, **Recommended pattern**, or per-location notes are vague, jargon-heavy, or hard to act on. Rewrite for clarity. Do not change which sites are listed; do not change the rule cited.
   - **Amend** — the finding is partly right. Drop locations that no longer match, fix wrong line numbers, narrow an over-broad scope claim, or correct a wrong rule citation (only when the **same** style file actually has the right rule — switching to a different style file is removal-plus-no-add, not an amendment).
   - **Remove** — the finding is wrong, the rule no longer exists, every cited location is invalid, or the finding contradicts a `[non-negotiable]` rule from the same style file. Wrap (do not delete) per Step 4.

Be conservative on removal. A finding that is merely awkwardly worded should be improved, not removed. Removal is for findings a competent human reviewer would reject outright.

## Step 4: Apply edits to EVALUATION.md

For findings you keep or improve in place, edit the relevant lines directly.

For findings you **amend**, edit them in place.

For findings you **remove**, do not delete the text. Replace the finding's body with the original text wrapped in marker comments:

```markdown
### N. [original title]

<!-- REMOVED-BY-REVIEW: [one-sentence reason] -->
**Style file**: `...` (original)
**Style rule**: ... (original)
**Locations** ...:
- ... (original)
**Recommended pattern**: ... (original)
<!-- /REMOVED-BY-REVIEW -->
```

The numbering of remaining findings is preserved — do **not** renumber. A removed finding still occupies its number; downstream tooling expects stable numbering across the eval/review/fix sequence.

## Step 5: Append the Review Log

Append a `## Review Log` section to the END of `EVALUATION.md`. Format:

```markdown
---

## Review Log

**Reviewed**: [YYYY-MM-DD]
**Findings reviewed**: [N]
**Improved**: [count]
**Amended**: [count]
**Removed**: [count]

### Actions

- **Finding 1** — kept | improved | amended | removed — [one-line reason]
- **Finding 2** — ...
[...one bullet per numbered finding, in order]
```

Every numbered finding gets exactly one bullet, even if you took no action (`kept`). The bullet's reason should be terse — one short clause. For improved/amended bullets, name what you changed (e.g. "narrowed Locations to the two sites that still match", "rewrote Recommended pattern to drop guide-jargon"). For removed bullets, name why (e.g. "rule cited does not exist in the loaded style file", "all four locations were already fixed in the source").

## Hard rules

- Do **not** add new findings. If, while reviewing, you notice an unrelated violation, ignore it — `/style_eval` will catch it on the next nightly.
- Do **not** edit source code. Do **not** edit any style guide file.
- Do **not** delete a removed finding's text — wrap it in `REMOVED-BY-REVIEW` markers.
- Do **not** renumber findings.
- Do **not** modify the `## Improvements` heading or any structural scaffolding above the findings.
- Findings inside `<!-- REMOVED-BY-REVIEW -->` blocks and the `## Review Log` section are reporting-only. They are for the human reviewing `/style_fix_review`, not for downstream agents to act on.
