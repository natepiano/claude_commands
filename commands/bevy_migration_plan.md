---
description: Generate project-specific Bevy migration plan by analyzing the current codebase
---

# Bevy Migration Plan Generator

Analyzes your current codebase against the Bevy migration checklist to generate a project-specific, actionable migration plan with code examples and specific file locations.

**Usage:** `/bevy_migration_plan <version>`
**Example:** `/bevy_migration_plan 0.17.0`

**Prerequisites:** Must run `/bevy_migration_checklist <version>` first to generate the checklist

**Output:** `.claude/bevy_migration/bevy-{version}-migration-plan.md` (project-local)

---

<ExecutionSteps>

**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <ParseArguments/>
**STEP 2:** Execute <ValidatePrerequisites/>
**STEP 3:** Execute <RunMigrationAnalyzer/>
**STEP 4:** Execute <PresentResults/>

</ExecutionSteps>

---

<ParseArguments>

**Extract version from arguments:**

The command receives `$ARGUMENTS` containing the Bevy version (e.g., "0.17.0").

**Set the following variables for use in subsequent steps:**

```
VERSION = $ARGUMENTS (e.g., "0.17.0")
CHECKLIST = ~/.claude/bevy_migration/bevy-${VERSION}-migration-checklist.md
PLAN_DOCUMENT = .claude/bevy_migration/bevy-${VERSION}-migration-plan.md
CODEBASE = $PWD (current working directory)
GUIDES_DIR = ~/rust/bevy-${VERSION}/release-content/migration-guides
```

**Validation:**
- If `$ARGUMENTS` is empty, output an error: "Error: Version argument required. Usage: /bevy_migration_plan <version> (e.g., /bevy_migration_plan 0.17.0)"
- Stop execution if version is missing

</ParseArguments>

---

<ValidatePrerequisites>

**Validate that the checklist exists:**

Check if the checklist file exists:

```bash
ls ${CHECKLIST}
```

**If checklist does NOT exist:**
- Show error: "Error: Migration checklist not found for Bevy ${VERSION}"
- Show instructions: "Please run: /bevy_migration_checklist ${VERSION} first"
- Stop execution

**If checklist exists:**
- Proceed to next step

</ValidatePrerequisites>

---

<RunMigrationAnalyzer>

**Create progress tracker:**

Use TodoWrite to track execution progress:
```
[
  {"content": "Run migration analyzer script", "status": "in_progress", "activeForm": "Running migration analyzer script"},
  {"content": "Present migration plan report", "status": "pending", "activeForm": "Presenting migration plan report"}
]
```

**Run the migration analyzer script:**

Use the Bash tool to execute the Python migration analyzer with the variables set in ParseArguments:

```bash
mkdir -p .claude/bevy_migration/
python3 ~/.claude/scripts/bevy_migration_analyzer.py \
  --checklist ${CHECKLIST} \
  --plan ${PLAN_DOCUMENT} \
  --codebase ${CODEBASE} \
  --guides ${GUIDES_DIR} \
  --output ${PLAN_DOCUMENT}
```

**Note:** The `--guides` parameter loads official Bevy migration examples to include in the report.

**The script will:**
1. Parse the checklist to extract all migration items and search patterns
2. Search the codebase using ripgrep with context (5 lines before/after each match)
3. Extract code snippets with surrounding context (NO line numbers - they become stale)
4. Apply smart filtering to reduce false positives
5. Load Bevy migration guide examples (from --guides directory)
6. Match checklist items to official Bevy examples
7. Cross-reference findings against the existing migration plan (if it exists)
8. Categorize items by impact (CRITICAL/HIGH/MEDIUM/LOW)
9. Categorize by update size based on occurrence counts:
   - 0 occurrences = NOT_FOUND
   - 1-5 = Minor update
   - 6-20 = Medium update
   - 21+ = Major update
10. Generate actionable migration report with:
    - Official Bevy before/after examples
    - Actual code snippets from your codebase
    - Greppable context for finding all occurrences
    - File-by-file breakdown of affected locations

**Progress will be shown on stderr as the script runs.**

**After script completes:**

Mark the first task as completed and the second as in_progress:
```
[
  {"content": "Run migration analyzer script", "status": "completed", "activeForm": "Running migration analyzer script"},
  {"content": "Present migration plan report", "status": "in_progress", "activeForm": "Presenting migration plan report"}
]
```

</RunMigrationAnalyzer>

---

<PresentResults>

**Present the report to the user:**

The script outputs a comprehensive migration plan with the following sections:

1. **Summary Statistics** - Overview of findings
2. **Items to ADD to Migration Plan** - Grouped by priority (Critical/High/Medium/Low)
3. **Items Already Covered** - Items found in code that are already in the plan
4. **Items Not Applicable** - Checklist items not found in the codebase

Review the report and discuss next steps with the user.

**After presenting the report:**

Mark the final task as completed:
```
[
  {"content": "Run migration analyzer script", "status": "completed", "activeForm": "Running migration analyzer script"},
  {"content": "Present migration plan report", "status": "completed", "activeForm": "Presenting migration plan report"}
]
```

</PresentResults>

---

## Expected Output

The script generates an actionable migration report with this structure:

### Summary Statistics
```
- Total checklist items: 195
- Items found in codebase: 45
- Already in migration plan: 30
- Need to add: 15
- Not applicable: 150
```

### Critical/High Priority Changes

For each change requiring action, the report includes:

```markdown
### Observer API: Rename Trigger<E> to On<E>

**Occurrences:** 40 locations across 17 files
**Priority:** HIGH
**Update Size:** Major

#### Official Bevy Migration Guide

**Before (Bevy 0.16):**
```rust
fn my_observer(trigger: Trigger<OnAdd, MyComponent>) {
    let entity = trigger.target();
}
```

**After (Bevy 0.17):**
```rust
fn my_observer(add: On<Add, MyComponent>) {
    let entity = add.entity;
}
```

#### Example from Your Codebase

**File:** `./crates/hana/src/movable/state/observers.rs`
**Context:** `pub fn on_movable_added(`

**Current Code:**
```rust
pub fn on_movable_added(
    trigger: Trigger<OnAdd, Movable>,
    mut commands: Commands,
) {
    let entity = trigger.target();
    // ...
}
```

#### Pattern to Find All Occurrences

```bash
rg "Trigger<OnAdd" --type rust
```

#### All Affected Locations

- `./crates/hana/src/movable/state/observers.rs` (14 occurrences)
  - pub fn on_movable_added(
  - pub fn on_movable_removed(
  - ... and 12 more
- `./crates/hana/src/movable/selection/observers.rs` (4 occurrences)
  - pub fn on_select_start(
  - ... and 3 more

---
```

### Items Already Covered
```
- [x] Observer API (Trigger → On) - Found in 40 locations, COVERED in migration plan
- [x] Event/Message split - Found in 15 locations, COVERED in migration plan
```

### Items Not Applicable
```
- Timer::paused() - NOT FOUND in codebase
- glTF animations - NOT FOUND in codebase
```

---

## Deliverables

The command produces an **actionable migration plan** with:

1. **Code Snippets with Context** - See exactly what needs to change (no line numbers - use context to grep)
2. **Official Bevy Examples** - Before/after from Bevy migration guides
3. **Greppable Patterns** - Commands to find all occurrences
4. **File-by-File Breakdown** - Every affected location with function context
5. **Priority Matrix** - What to tackle first (CRITICAL → HIGH → MEDIUM → LOW)
6. **Coverage Analysis** - What's already in the plan vs what needs to be added

---

## Key Features

✅ **Actionable** - Developers can see exactly what to change
✅ **No Stale Line Numbers** - Uses greppable code context instead
✅ **Official Examples** - Integrates Bevy migration guide examples
✅ **Smart Filtering** - Reduces false positives (e.g., distinguishes entity Index from array indexing)
✅ **Complete Coverage** - Scans 195+ checklist items
✅ **Prioritized** - Focus on breaking changes first
✅ **Project-Specific** - Analyzes your actual codebase
