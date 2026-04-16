---
description: Evaluate a Rust project against the style guide and write EVALUATION.md with top improvements
---

**IMPORTANT**: Do NOT modify any source code. This is a read-only evaluation.

## Arguments
- `$ARGUMENTS` is the absolute path to a Rust project root (must contain a `Cargo.toml`)

## Step 1: Load the style guide (shuffled)

Run:

```bash
zsh ~/.claude/scripts/load-rust-style.sh --shuffle --project-root "$ARGUMENTS"
```

This loads the shared style guide plus any repo-local `docs/style/*.md` files, filtered for the project type (bevy rules excluded for non-bevy projects), in **random order** so evaluations rotate across rules over multiple nightly runs.

The output ends with a `=== STYLE_CHECKLIST ===` section listing every rule by number and name. Rules may be annotated with `[non-negotiable]` and/or `[group: name]`. This is your evaluation order — work through it sequentially.

If you need exact style file paths for citations, run:

```bash
zsh ~/.claude/scripts/load-rust-style.sh --list-files --shuffle --project-root "$ARGUMENTS"
```

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

## Step 4: Evaluate — systematic sequential walk

Work through the `=== STYLE_CHECKLIST ===` from Step 1 in two phases:

1. Evaluate every rule annotated with `[non-negotiable]` first, in checklist order.
2. Then evaluate the remaining rules, one rule at a time, in checklist order.

Non-negotiable rules are overarching constraints. Findings from them do **not** count against the 5-finding cap for new findings, and they constrain what later rules may recommend.

### Grouped rules

Some rules in the checklist are annotated with `[group: name]`. These rules are **cross-referential** — they form a decision framework where one rule's guidance depends on understanding the others.

When you encounter a grouped rule:
- Evaluate **all rules in that group** together before moving to the next ungrouped rule
- The entire group counts as **one checklist item** — not N items. A group of 3 rules consumes 1 slot, not 3
- Findings from grouped rules still count individually toward the 5-finding cap (each violation is one finding)

### Non-negotiable rules

Rules annotated with `[non-negotiable]` are mandatory, overarching constraints on the entire evaluation.

When you encounter a non-negotiable rule:
- Evaluate it even if you have already found 5 other new findings
- Record every genuine violation you find; these do **not** consume the 5-finding budget
- Treat the rule as binding on later findings from other rules
- Do **not** recommend a pattern under another style rule if that recommendation would violate a non-negotiable rule

If a non-negotiable rule is also grouped, evaluate the entire group together during the non-negotiable phase. The group still counts as one checklist item for `Rules checked`.

### Evaluation procedure

For each rule (or group of rules):
1. Read the full rule content (already loaded in Step 1)
2. Check the **entire codebase** you surveyed in Step 2 for violations of that specific rule
3. If you find a violation:
   - Confirm it is not already carried forward from Step 3
   - Confirm it is not excluded by Step 3.5
   - If it's a genuine new finding, **write it immediately**
   - If the rule is not marked `[non-negotiable]`, increment your 5-finding budget count
   - If the rule is marked `[non-negotiable]`, do **not** increment your 5-finding budget count
   - **Stop after 5 new findings from rules that are not marked `[non-negotiable]`** — do not continue checking more normal rules
4. If no violation: move to the next rule (or next group)

This ensures:
- Every rule gets a fair chance to surface (the shuffle ensures different rules go first each run)
- Non-negotiable rules are always evaluated first and never crowded out by the normal finding cap
- Grouped rules always get evaluated with their full context
- You don't waste effort scanning for more violations after hitting the cap
- Coverage rotates naturally across nightly runs

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
