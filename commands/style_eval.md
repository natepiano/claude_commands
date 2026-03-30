---
description: Evaluate a Rust project against the style guide and write EVALUATION.md with top 5 improvements
---

**IMPORTANT**: Do NOT modify any source code. This is a read-only evaluation.

## Arguments
- `$ARGUMENTS` is the absolute path to a Rust project root (must contain a `Cargo.toml`)

## Step 1: Load the style guide

Read every file in `~/rust/nate_style/rust/` to understand the full set of style rules.

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

## Step 4: Evaluate

Compare what you've read against every rule in the style guide. Look for systemic patterns, not one-off issues. Consider:
- Import organization and style
- Visibility practices
- Lint configuration
- Code patterns (error handling, iterators, trait usage, etc.)
- Bevy-specific conventions (if applicable)
- Project setup conventions

Identify up to 5 **new** violations not already carried forward from the existing evaluation.

## Step 5: Write EVALUATION.md

Write `$ARGUMENTS/EVALUATION.md` combining carried-forward findings and new findings.

If there are **no violations** (nothing carried forward and nothing new), write:

```markdown
# Style Evaluation

**Project**: [project name]
**Date**: [YYYY-MM-DD]
**Files reviewed**: [count]

## No violations found

This project fully conforms to the style guide.
```

Otherwise, write:

```markdown
# Style Evaluation

**Project**: [project name]
**Date**: [YYYY-MM-DD]
**Files reviewed**: [count]

## Improvements

### 1. [Title]

**Style rule**: [which rule from the guide]
**Current pattern**: [what the code does now, with 1-2 concrete examples showing file paths and line numbers]
**Recommended pattern**: [what it should look like]
**Scope**: [how many files / how widespread]

### 2. [Title]

[same structure]

[...continue numbering for all findings]

```

Do NOT include an "Overall Assessment" section — just list the findings.

Requirements for each finding:
- Rank by impact: most files affected and most deviation from the guide comes first
- Be specific: include actual file paths and line numbers from the project
- Be actionable: someone should be able to act on each item without re-reading the style guide
- Only flag things that genuinely violate the style guide — do not invent rules
