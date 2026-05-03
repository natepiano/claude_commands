---
description: Ad hoc targeted style evaluation — check a directory scope against a specific subset of style guidelines (no nightly scheduler, no budget, no history writes)
---

**IMPORTANT**: Do NOT modify any source code. This is a read-only evaluation.

## Arguments

`$ARGUMENTS` is a whitespace-separated list in this order:

```
<project-root> <scope-glob> <guideline-1> [<guideline-2> ...]
```

- `<project-root>` — absolute path to the Rust project root (must contain `Cargo.toml`)
- `<scope-glob>` — a directory, file, or glob relative to the project root (e.g. `src/types/`, `src/types/**/*.rs`, `examples/benchmark.rs`). The review focuses on files matching this scope, plus any files that the style guidelines themselves point into as context (e.g. relocation targets).
- `<guideline-...>` — one or more guideline identifiers. Accepts bare stems (`when-to-split-a-module`), filenames (`when-to-split-a-module.md`), or guideline ids (`rust/when-to-split-a-module.md`). `.md` is optional.

If `$ARGUMENTS` is empty or has fewer than three tokens, print usage and stop:

```text
Usage: /focused_eval <project-root> <scope-glob> <guideline-1> [<guideline-2> ...]

Example:
  /focused_eval ~/rust/bevy_liminal src/types/ \
      when-to-split-a-module \
      types-live-with-their-behavior \
      name-submodules-after-anchor-types split-by-type-ownership
```

## Step 1: Resolve the guidelines

Run the read-only `focused-eval` helper — it does **not** touch the nightly scheduler's pending or history state:

```bash
python3 ~/.claude/scripts/nightly/style_history.py focused-eval \
    --project-root "<project-root>" \
    --guideline <guideline-1> \
    [--guideline <guideline-2> ...]
```

The helper emits one JSON object per line, one per requested guideline. Each object has:

- `unit_id` — the guideline id (e.g. `rust/when-to-split-a-module.md`)
- `display_name` — the guideline's heading text
- `guideline_ids` — always a one-element list equal to `[unit_id]`
- `see_also_guideline_ids` — the resolved guideline ids of any `see_also` wikilinks in that guideline's frontmatter

If any requested guideline is unresolvable, the helper exits non-zero and prints the offending input. Fix the spelling and rerun.

## Step 2: Resolve the style-file paths for citations

Run:

```bash
zsh ~/.claude/scripts/load-rust-style.sh --list-files --project-root "<project-root>"
```

Match each `guideline_id` against the output to get the absolute style-file path — you'll need this to cite findings in Step 5.

## Step 3: Read the scope

Read every `.rs` file under `<scope-glob>` within the project root. For each file, read enough to understand types, impls, and the functions defined at each visibility level.

## Step 4: Read each guideline and its see_alsos, then review

For each unit emitted in Step 1, in the order emitted:

1. Read the guideline file (from the path resolved in Step 2).
2. Read every file in `see_also_guideline_ids` — the style guide treats these as binding context for this review. Do not record findings *against* the see_also'd guidelines; they get their own unit on their own turn.
3. Evaluate the scope files from Step 3 against the unit's rule.
4. Allow the rule to point *outside* the scope when the rule is inherently cross-module (e.g. relocation rules that require knowing what other modules already own a type's behavior). In that case, read the named sibling file(s) too and cite them as evidence — but still scope findings to work the user has to do inside `<scope-glob>`.

## Step 5: Write the report inline

Output a markdown report directly in chat — do **not** write an `EVALUATION.md` file, do **not** touch `style_history`, do **not** commit anything.

Report format:

```markdown
# Focused Eval — <project-root-basename>

**Scope:** `<scope-glob>`
**Guidelines:** `<N>` requested
**Files in scope:** <N>

## <1. Display name of guideline 1>

**Style file**: `<absolute path>`
**see_also context**: <list of see_also ids or "none">

<findings for this guideline, or "No finding.">

## <2. Display name of guideline 2>

...
```

For each guideline, a "finding" is a concrete violation the user could act on. A finding represents one guideline and must enumerate **every** instance of the violation across the in-scope files — not a sample. Tool precedence for the scan:
- **LSP** (`workspaceSymbol`, `findReferences`, `documentSymbol`, `hover`) for any semantic query — types, signatures, trait impls, callers. ripgrep cannot answer those structurally.
- **ripgrep** for textual queries (keywords, attribute strings, identifier patterns).
- **Read source files** only to verify a specific site found via the tools above.

- `**Locations**` (every violation found in scope):
  - `path/to/file.rs:42` — [optional brief note about this site, only if it differs materially from the others]
  - `path/to/other.rs:15` — [...]
  - `path/to/third.rs:88-94` — [...]
- `**Recommended pattern**:` what to change (written once, applies to every location)

If a guideline produces nothing, write `No finding.` and move on. Do not invent violations to fill space.

## Step 6: Cross-check (optional, if multiple guidelines interact)

After the per-guideline sections, add one short closing paragraph only if the findings across guidelines reinforce a single refactoring plan. Otherwise omit.

## Rules

- **Read-only.** Never edit source. Never touch `style_history` beyond the one `focused-eval` call.
- **No invented rules.** Only flag what the guideline's text supports.
- **No budget.** The nightly scheduler is bypassed entirely; evaluate every requested guideline.
- **No EVALUATION.md.** This is ad hoc — findings live in chat, not on disk.
