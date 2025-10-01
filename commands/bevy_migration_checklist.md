---
description: Generate comprehensive Bevy migration checklist from official migration guides
---

# Bevy Migration Checklist Generator

Generates a comprehensive migration checklist by parsing official Bevy migration guides from the Bevy repository.

**Usage:** `/bevy_migration_checklist <version>`
**Example:** `/bevy_migration_checklist 0.17.0`

**Output:** `~/.claude/bevy_migration/bevy-{version}-migration-checklist.md`

---

<ExecutionSteps>

**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <ParseArguments/>
**STEP 2:** Execute <ValidateVersion/>
**STEP 3:** Execute <CloneBevyRepo/>
**STEP 4:** Execute <GenerateChecklist/>
**STEP 5:** Execute <PresentResults/>

</ExecutionSteps>

---

<ParseArguments>

**Extract version from arguments:**

The command receives `$ARGUMENTS` containing the Bevy version (e.g., "0.17.0").

**Set the following variables:**

```
VERSION = $ARGUMENTS (e.g., "0.17.0")
BEVY_REPO_DIR = ~/rust/bevy-${VERSION}
GUIDES_DIR = ~/rust/bevy-${VERSION}/release-content/migration-guides
OUTPUT_FILE = ~/.claude/bevy_migration/bevy-${VERSION}-migration-checklist.md
```

**Validation:**
- If `$ARGUMENTS` is empty, output error: "Error: Version argument required. Usage: /bevy_migration_checklist <version> (e.g., /bevy_migration_checklist 0.17.0)"
- Stop execution if version is missing

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
   ls ${GUIDES_DIR}
   ```
   - If directory doesn't exist or is empty, show error and stop

**Progress messages:**
- "Cloning Bevy repository..."
- "Checking out version v${VERSION}..."
- "Found migration guides directory"

</CloneBevyRepo>

---

<GenerateChecklist>

**Run the checklist generator script:**

1. **Create output directory:**
   ```bash
   mkdir -p ~/.claude/bevy_migration/
   ```

2. **Run the generator:**
   ```bash
   python3 ~/.claude/scripts/bevy_migration_checklist_generator.py \
     --version ${VERSION} \
     --guides-dir ${GUIDES_DIR} \
     --output ${OUTPUT_FILE}
   ```

**The script will:**
- Parse all `*.md` files in the migration-guides directory
- Extract migration items, code examples, and search patterns
- Generate a comprehensive checklist markdown file
- Save to the output location

**Error handling:**
- If script fails, show the error output
- Common errors:
  - Guides directory not found
  - Permission issues
  - Python errors

</GenerateChecklist>

---

<PresentResults>

**Present the results to the user:**

Show a summary of what was generated:

```
✓ Bevy ${VERSION} Migration Checklist Generated

Location: ${OUTPUT_FILE}

Summary:
- Parsed X migration guides
- Generated X checklist items
- Included official code examples and search patterns

Next Steps:
1. Review the checklist at the location above
2. Run /bevy_migration_plan ${VERSION} to generate a project-specific migration plan

The checklist is reusable across all your Bevy projects.
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

- The checklist is generated from official Bevy migration guides
- It's saved globally in `~/.claude/bevy_migration/` for reuse across projects
- The Bevy repo is cloned to `~/rust/bevy-{version}/` for reference
- Run this command once per Bevy version
- After generating the checklist, use `/bevy_migration_plan` to create project-specific plans
