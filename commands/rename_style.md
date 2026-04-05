# Rename Style

Rename a shared style file and update all historical log entries and active evaluations.

**Arguments**: `$ARGUMENTS` — `old_name.md new_name.md`

## Instructions

Parse `$ARGUMENTS` to extract `old_name` and `new_name`. Both should end in `.md`.

If `$ARGUMENTS` is empty or doesn't contain exactly two arguments, show usage and stop:
```
Usage: /rename_style old_name.md new_name.md
```

### Step 1: Validate

1. Check that `~/rust/nate_style/rust/{old_name}` exists. If not, error: "Source style file not found: {old_name}"
2. Check that `~/rust/nate_style/rust/{new_name}` does NOT exist. If it does, error: "Target style file already exists: {new_name}"

### Step 2: Rename the file

```bash
mv ~/rust/nate_style/rust/{old_name} ~/rust/nate_style/rust/{new_name}
```

### Step 3: Update log.jsonl

Read `~/rust/nate_style/usage/log.jsonl`. For each line where `style_id` is `shared:rust/{old_name}`:
- Update `style_id` to `shared:rust/{new_name}`
- Update `style_file` to `{new_name}`

Write the updated content back to `log.jsonl`. Count how many entries were updated.

### Step 4: Update active EVALUATION.md files

Scan `~/rust/*/EVALUATION.md` for references to the old style file path. Update any matches to use the new filename. Count how many files were updated.

### Step 5: Update wikilinks across the Obsidian vault

Strip `.md` from both old and new names to get the wikilink stems (e.g., `cargo-toml-lints`).

Search `~/rust/nate_style/` recursively for files containing `[[{old_stem}]]` or `[[{old_stem}|`. Replace all occurrences:
- `[[{old_stem}]]` → `[[{new_stem}]]`
- `[[{old_stem}|` → `[[{new_stem}|`

This covers `style_report.md`, diary archives, and any other Obsidian files referencing the style.

Count how many files were updated.

### Step 6: Regenerate reports

Run: `python3 ~/rust/nate_style/usage/summary.py --generate`

### Step 7: Report

Display a summary:
```
Renamed: {old_name} → {new_name}
Log entries updated: {count}
EVALUATION.md files updated: {count}
Obsidian wikilinks updated: {count} files
```
