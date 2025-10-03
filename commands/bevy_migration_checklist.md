---
description: Generate comprehensive Bevy migration checklist from official migration guides
---

# Bevy Migration Checklist Generator

Generates a comprehensive migration checklist by parsing official Bevy migration guides from the Bevy repository.

**Usage:** `/bevy_migration_checklist <version>`
**Example:** `/bevy_migration_checklist 0.17.0`

**Output:** `~/.claude/bevy_migration/bevy-{version}-checklist.md`

---

<ExecutionSteps>

**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <ParseArguments/>
**STEP 2:** Execute <ValidateVersion/>
**STEP 3:** Execute <DisplayExecutionPlan/>
**STEP 4:** Execute <CloneBevyRepo/>
**STEP 5:** Execute <GenerateChecklist/>
**STEP 6:** Execute <PresentResults/>

</ExecutionSteps>

---

<ParseArguments>

**Extract version from arguments:**

The command receives `$ARGUMENTS` containing the Bevy version (e.g., "0.17.0").

**Validation:**
- If `$ARGUMENTS` is empty, output error: "Error: Version argument required. Usage: /bevy_migration_checklist <version> (e.g., /bevy_migration_checklist 0.17.0)"
- Stop execution if version is missing

**Define execution values:**

Extract the version and derive all paths. When executing bash commands in subsequent steps, directly substitute these values:

```
VERSION = $ARGUMENTS (e.g., "0.17.0")
BEVY_REPO_DIR = ~/rust/bevy-${VERSION}
GUIDES_DIR = ~/rust/bevy-${VERSION}/release-content/migration-guides

# Final output directory and file
OUTPUT_DIR = ~/.claude/bevy_migration
FINAL_OUTPUT = ${OUTPUT_DIR}/bevy-${VERSION}-checklist.md
```

</ParseArguments>

---

<ValidateVersion>

**Validate that the version exists on GitHub:**

Use the GitHub CLI to check if the release tag exists:

```bash
gh api repos/bevyengine/bevy/releases/tags/v${VERSION}
```

**If successful:**
- Tag exists, proceed to next step

**If error (404 or other):**
- Show error message: "Error: Bevy version ${VERSION} not found on GitHub"
- Try to list available recent versions to help the user:
  ```bash
  gh api repos/bevyengine/bevy/releases --jq '.[0:10] | .[] | .tag_name'
  ```
- Show message: "Available recent versions: ..."
- Stop execution

**Note:** The tag format is `v{VERSION}` (e.g., `v0.17.0`)

</ValidateVersion>

---

<DisplayExecutionPlan>

**Display the execution plan to the user:**

After validating the version exists, show the user exactly what will be executed:

```
═══════════════════════════════════════════════════════════
Bevy ${VERSION} Migration Checklist Generation Plan
═══════════════════════════════════════════════════════════

Version: ${VERSION}
Tag: v${VERSION}

Processing Approach:
  Single-pass deterministic Python generation
  Direct extraction from official migration guides
  No LLM enhancement (guarantees accuracy)

Final Output:
  ~/.claude/bevy_migration/bevy-${VERSION}-checklist.md

Execution Steps:
  1. Remove existing Bevy repo (if present)
     → rm -rf ~/rust/bevy-${VERSION}

  2. Clone Bevy repository
     → git clone https://github.com/bevyengine/bevy.git ~/rust/bevy-${VERSION}

  3. Checkout version tag v${VERSION}
     → git -C ~/rust/bevy-${VERSION} checkout v${VERSION}

  4. Generate checklist (Python)
     → python3 ~/.claude/scripts/bevy_migration_checklist_generator.py
     → Processes all migration guides deterministically

═══════════════════════════════════════════════════════════
Proceeding with execution...
═══════════════════════════════════════════════════════════
```

</DisplayExecutionPlan>

---

<CloneBevyRepo>

**Clone the Bevy repository to version-specific directory:**

1. **Remove existing directory if it exists:**
   ```bash
   rm -rf ${BEVY_REPO_DIR}
   ```

2. **Clone the repository:**
   ```bash
   git clone https://github.com/bevyengine/bevy.git ${BEVY_REPO_DIR}
   ```

3. **Checkout the specific version tag:**
   ```bash
   git -C ${BEVY_REPO_DIR} checkout v${VERSION}
   ```

4. **Verify migration guides exist:**
   ```bash
   ~/.claude/scripts/verify_migration_guides.sh ${GUIDES_DIR}
   ```

**Progress messages:**
- "Cloning Bevy repository..."
- "Checking out version v${VERSION}..."
- "Verifying migration guides..."

</CloneBevyRepo>

---

<GenerateChecklist>

**Run the checklist generator (single-pass Python)**

1. **Ensure output directory exists:**
   ```bash
   mkdir -p ${OUTPUT_DIR}
   ```

2. **Run the generator:**
   ```bash
   python3 ~/.claude/scripts/bevy_migration_checklist_generator.py \
     --version ${VERSION} \
     --guides-dir ${GUIDES_DIR} \
     --output ${FINAL_OUTPUT}
   ```

**The script will:**
- Parse all `*.md` files in the migration-guides directory
- Extract titles, PR numbers, descriptions, bullet points, code blocks
- Identify change types (rename, remove, move, etc.)
- Generate actionable checklist items from code diffing and descriptions
- Extract and prioritize search patterns (old code first)
- Add contextual guidance based on change type
- Write final checklist directly to output

**Output:**
- `${FINAL_OUTPUT}` - Complete migration checklist

**Progress messages:**
- "Parsing X migration guides..."
- "✓ Parsed X migration guides"
- "✓ Final checklist generated: ${FINAL_OUTPUT}"
- "✓ Total sections: X"

</GenerateChecklist>

---

<PresentResults>

**Present the results to the user:**

Show a summary of what was generated:

```
═══════════════════════════════════════════════════════════
✓ Bevy ${VERSION} Migration Checklist Generated
═══════════════════════════════════════════════════════════

Final Checklist:
  ${FINAL_OUTPUT}

Processing:
- Parsed all migration guides from official Bevy repository
- Generated deterministic, accurate checklist
- 100% faithful to source migration guides

Features:
✓ Complete coverage of all migration guides
✓ Actionable checklist items with specific changes
✓ Search patterns to find affected code
✓ Required vs optional changes identified
✓ Official code examples preserved
✓ No hallucinated content (deterministic generation)

Next Steps:
1. Review the checklist: ${FINAL_OUTPUT}
2. Run /bevy_migration_plan ${VERSION} to create a project-specific migration plan

The checklist is reusable across all your Bevy projects.
═══════════════════════════════════════════════════════════
```

</PresentResults>

---

## What Gets Generated

The checklist includes for each migration item:

- **Title**: Migration guide title
- **Pull Requests**: Links to the PRs
- **Description**: What changed and why
- **Checklist Items**: Specific tasks to complete
- **Search Patterns**: Patterns to search for in your code
- **Official Examples**: Before/after code from Bevy migration guides

**Example Output:**

```markdown
## Observer Triggers

**Pull Requests:** #19440, #19596

The `Trigger` type used inside observers has been renamed to `On` for a cleaner API.

- [ ] Rename `Trigger<E>` to `On<E>`
- [ ] Rename `OnAdd` → `Add`, `OnInsert` → `Insert`, etc.
- [ ] Handle `target()` now returns `Option<Entity>`

**Search Patterns:** `Trigger<`, `OnAdd`, `OnInsert`, `trigger.target()`

**Official Example:**

```rust
// Old
commands.add_observer(|trigger: Trigger<OnAdd, Player>| {
    info!("Spawned player {}", trigger.target());
});

// New
commands.add_observer(|trigger: On<Add, Player>| {
    info!("Spawned player {}", trigger.target());
});
```
```

---

## Notes

- The checklist is generated from official Bevy migration guides using deterministic Python extraction
- No LLM enhancement ensures 100% accuracy and zero hallucination
- It's saved globally in `~/.claude/bevy_migration/` for reuse across projects
- The Bevy repo is cloned to `~/rust/bevy-{version}/` for reference
- Run this command once per Bevy version
- After generating the checklist, use `/bevy_migration_plan` to create project-specific plans
