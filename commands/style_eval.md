---
description: Evaluate a Rust project against the style guide and write EVALUATION.md with top improvements
---

**IMPORTANT**: Do NOT modify any source code. This is a read-only evaluation.

## Arguments
- `$ARGUMENTS` is the absolute path to a Rust project root (must contain a `Cargo.toml`)

## Step 1: Load the full style guide

Run:

```bash
zsh ~/.claude/scripts/load-rust-style.sh --project-root "$ARGUMENTS"
```

This loads the shared style guide plus any repo-local `docs/style/*.md` files, filtered for the project type.

The output ends with a `=== STYLE_CHECKLIST ===` section listing every rule by number and name. Rules may be annotated with `[non-negotiable]`. This is your evaluation order — work through it sequentially.

If you need exact style file paths for citations, run:

```bash
zsh ~/.claude/scripts/load-rust-style.sh --list-files --project-root "$ARGUMENTS"
```

## Step 1.5: Use the nightly selection helper

You must pull evaluation units one at a time from:

```bash
python3 ~/.claude/scripts/nightly/style_history.py next-unit --project-root "$ARGUMENTS"
```

The helper returns JSON.

Rules:
- `status=next` means review exactly that returned unit next
- `status=complete` means stop immediately
- `stop_reason=budget_reached` means you have reached 5 scored units
- `stop_reason=exhausted` means there are no unseen eligible units left for this run
- `non_negotiable_guideline_ids` are binding on every unit review and are returned every time
- each unit is exactly one guideline file — its `unit_id` equals the `guideline_id`
- `see_also_guideline_ids` on a unit lists additional guidelines whose content you must consult as context when reviewing this unit — do NOT record results for them (they are scored on their own separate review cycle)

After reviewing a unit, you must record its result immediately with:

```bash
python3 ~/.claude/scripts/nightly/style_history.py record-unit --project-root "$ARGUMENTS" --results /tmp/style-eval-results.json
```

The results JSON must have this shape:

```json
{
  "unit_id": "rust/when-to-split-a-module.md",
  "results": [
    {
      "guideline_id": "rust/when-to-split-a-module.md",
      "outcome": {
        "status": "no_findings"
      }
    }
  ]
}
```

Recording rules:
- use `outcome.status = no_findings` when that guideline produced no finding
- use `finding_source = new | carried_forward` when that guideline produced a finding to keep in `EVALUATION.md`
- if the guideline produces a finding, the unit counts as `1`; otherwise it counts as `0`
- do not review the next unit until the current unit has been recorded

## Step 2: Survey the project

Read the project's `Cargo.toml` at `$ARGUMENTS/Cargo.toml` to understand the project structure (workspace members, dependencies, features).

Then find and read all `.rs` source files under `$ARGUMENTS/src/`, `$ARGUMENTS/examples/`, and under any workspace member `src/` and `examples/` directories. For large projects, prioritize:
- `lib.rs` and `main.rs` files
- Module root files (`mod.rs`)
- Files with the most code

Read enough to form a thorough understanding of the codebase's patterns. Aim for at least 15-20 source files or all files if fewer exist.

## Step 3: Review existing EVALUATION.md (if present)

If `$ARGUMENTS/EVALUATION.md` exists, read it. For each previously listed improvement:
- **Verify** whether the issue still exists in the current code (check the specific files and line numbers cited)
- **Keep** it if the violation is still present (update file paths and line numbers if they've shifted)
- **Remove** it if the code has been fixed

Carry forward any still-valid findings — they do not count against the limit of new findings in Step 4.

## Step 3.5: Exclude findings already being fixed in a worktree

Derive the worktree evaluation path: take the project directory name, append `_style_fix`, and check for `EVALUATION.md` there. For example, if `$ARGUMENTS` is `~/rust/my_project`, check `~/rust/my_project_style_fix/EVALUATION.md`.

If that file exists, read it. These findings are already being addressed in a style-fix branch. When evaluating in Step 4, **do not re-discover** any finding that matches a worktree finding by title or by the same style rule applied to the same files. This prevents duplicate work between the primary evaluation and the in-progress worktree fixes.

## Step 4: Evaluate only the selected guideline units

Loop until the helper returns `status=complete`.

For each returned unit:
1. Read the full rule content for that selected unit
2. Read the content of any `see_also_guideline_ids` on the unit as review context — apply the selected unit's rule, informed by that context, but do not record findings against the see_also'd guidelines (they get their own review cycle)
3. Re-read the returned `non_negotiable_guideline_ids` and treat them as binding for this unit
4. Check the relevant codebase for violations of that selected unit
5. Record the result for the unit's single guideline:
   - if it has no issue, record `outcome.status = no_findings`
   - if it has an issue that is still valid from Step 3, keep it and record `finding_source = carried_forward`
   - if it has a genuinely new issue, record `finding_source = new`
   - if it matches an in-progress `_style_fix` finding from Step 3.5, do not include it
6. Record the unit immediately with `record-unit`
7. Then ask the helper for the next unit

Important:
- do not invent your own stopping rule
- do not stop after 5 helper calls
- keep pulling units until the helper says `budget_reached` or `exhausted`
- `no_findings` units do not consume the 5-unit scored budget

## Step 5: Write EVALUATION.md

Write `$ARGUMENTS/EVALUATION.md` combining carried-forward findings and new findings.

If there are **no violations** (nothing carried forward and nothing new), write:

```markdown
# Style Evaluation

**Project**: [project name]
**Date**: [YYYY-MM-DD]
**Files reviewed**: [count]
**Rules checked**: [how many rules were checked before stopping or exhausting the list]

## No violations found

This project fully conforms to the style guide.
```

Otherwise, write:

```markdown
# Style Evaluation

**Project**: [project name]
**Date**: [YYYY-MM-DD]
**Files reviewed**: [count]
**Rules checked**: [how many rules were checked before stopping or exhausting the list]

## Improvements

### 1. [Title]

**Style file**: `[full path from the loader file list]`
**Style rule**: [which rule from the guide]
**Current pattern**: [what the code does now, with 1-2 concrete examples showing file paths and line numbers]
**Recommended pattern**: [what it should look like]
**Scope**: [how many files / how widespread]

### 2. [Title]

[same structure, including Style file]

[...continue numbering for all findings]

```

Do NOT include an "Overall Assessment" section — just list the findings.

Requirements for each finding:
- Rank by impact: most files affected and most deviation from the guide comes first
- Be specific: include actual file paths and line numbers from the project
- Be actionable: someone should be able to act on each item without re-reading the style guide
- Only flag things that genuinely violate the style guide — do not invent rules
- Always include the full path to the exact style guide file each finding comes from, using the loader file list (e.g., `~/rust/nate_style/rust/one-use-per-line.md` or `$ARGUMENTS/docs/style/frontend-boundaries.md`)
