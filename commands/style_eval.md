---
description: Evaluate a Rust project against the style guide and write EVALUATION.md with top improvements
---

**IMPORTANT**: Do NOT modify any source code. This is a read-only evaluation.

## Arguments
- `$ARGUMENTS` is the absolute path to a Rust project root (must contain a `Cargo.toml`)
- `__SELECTION_MANIFEST__` is the absolute path to the nightly selection manifest for this project

## Step 1: Load the full style guide

Run:

```bash
zsh ~/.claude/scripts/load-rust-style.sh --project-root "$ARGUMENTS"
```

This loads the shared style guide plus any repo-local `docs/style/*.md` files, filtered for the project type.

The output ends with a `=== STYLE_CHECKLIST ===` section listing every rule by number and name. Rules may be annotated with `[non-negotiable]` and/or `[group: name]`. This is your evaluation order — work through it sequentially.

If you need exact style file paths for citations, run:

```bash
zsh ~/.claude/scripts/load-rust-style.sh --list-files --project-root "$ARGUMENTS"
```

## Step 1.5: Read the nightly selection manifest

Read the JSON file at `__SELECTION_MANIFEST__`.

It contains the specific guideline units selected for this run. Only these units may produce **new** findings in this evaluation.

Selection rules:
- a `rule` unit is a single style rule
- a `group` unit must be evaluated as one unit across all of its member style files
- a `non_negotiable` budget kind still counts as selected work for this run even though it consumes `0` nightly budget
- the manifest order is authoritative for this run

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

Use the selection manifest from Step 1.5.

For this nightly run:
- carry forward still-valid findings from the previous `EVALUATION.md`
- discover **new** findings only from the selected units in the manifest
- do not search for new findings outside the selected units

Evaluation procedure for each selected unit, in manifest order:
1. Read the full rule content for that selected unit
2. If the unit is a group, evaluate every member rule in that group together
3. Check the relevant codebase for violations of that selected unit
4. If you find a violation:
   - confirm it is not already carried forward from Step 3
   - confirm it is not excluded by Step 3.5
   - write it as a new finding
5. If the selected unit is non-negotiable:
   - treat it as binding on all recommendations
   - do not recommend any change that would violate it

For this prompt, the manifest already handles nightly budgeting. Do not apply any separate "stop after 5 findings" rule inside the evaluation itself.

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
