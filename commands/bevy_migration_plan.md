---
description: Generate project-specific Bevy migration plan by analyzing the current codebase against official migration guides
---

# Bevy Migration Plan Generator

Analyzes your codebase against official Bevy migration guides using a two-pass parallel subagent strategy to generate a comprehensive, project-specific migration plan.

**Usage:** `/bevy_migration_plan <version> [path]`
**Examples:**
- `/bevy_migration_plan 0.17.1` (analyze current directory)
- `/bevy_migration_plan 0.17.1 ~/rust/my_game` (analyze specific project)

**Output:**
- `{path}/.claude/bevy_migration/bevy-{version}-guides.md` (combined guides)
- `{path}/.claude/bevy_migration/bevy-{version}-migration-plan.md` (migration plan)

---

<ExecutionSteps>

**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <ParseArguments/>
**STEP 2:** Execute <CloneRepository/>
**STEP 3:** Execute <DependencyCompatibilityCheck/>
**STEP 4:** Execute <Pass1_ApplicabilityFilter/>
**STEP 5:** Execute <Pass2_DetailedAnalysis/>
**STEP 6:** Execute <MergeMigrationPlan/>
**STEP 7:** Execute <PresentResults/>

</ExecutionSteps>

---

<ParseArguments>

**Extract version and optional path from arguments:**

The command receives `$ARGUMENTS` containing:
- First argument: Bevy version (e.g., "0.17.1") - REQUIRED
- Second argument: Path to codebase to analyze (e.g., "~/rust/my_game") - OPTIONAL

**Validation:**
- If `$ARGUMENTS` is empty, output error: "Error: Version argument required. Usage: /bevy_migration_plan <version> [path]"
- Stop execution if version is missing

**Parse arguments:**
- Split `$ARGUMENTS` on whitespace
- First token = VERSION
- Second token (if present) = CODEBASE_PATH
- If CODEBASE_PATH not provided, use `$PWD`

**Define execution values:**

```
VERSION = [first argument] (e.g., "0.17.1")
CODEBASE = [second argument or $PWD] (e.g., "~/rust/my_game" or current directory)
BEVY_REPO_DIR = ${HOME}/rust/bevy-${VERSION} (global, reusable across projects)
GUIDES_DIR = ${BEVY_REPO_DIR}/release-content/migration-guides
MIGRATION_PLAN = ${CODEBASE}/.claude/bevy_migration/bevy-${VERSION}-migration-plan.md
```

**Path Expansion Note:**
- When constructing bash commands, ensure tildes are expanded to actual paths
- If CODEBASE contains tilde (e.g., user provides "~/rust/my_game"), expand it before using in bash commands
- Use `${HOME}` instead of `~` in all template definitions to avoid ambiguity
- BEVY_REPO_DIR already uses `${HOME}` for reliable path resolution across all contexts

**Note:** The Bevy repository is cloned to a global location (`${HOME}/rust/bevy-${VERSION}`) to avoid duplicate clones. The migration plan is saved in the target project's `.claude/bevy_migration/` directory.

</ParseArguments>

---

<UpdateTodoProgress old_task="..." new_task="...">
Use TodoWrite tool to update the todo list - mark "${old_task}" as completed and "${new_task}" as in_progress.
</UpdateTodoProgress>

---

<Pass2OutputTemplate>
Generate markdown section with this EXACT structure:

```markdown
## [Title from guide or filename without .md]

**Guide File:** `${BEVY_REPO_DIR}/${GUIDE_FILE_PATH}`
**Requirement Level:** [REQUIRED/HIGH/MEDIUM/LOW]
**Occurrences:** [X] locations across [Y] files

### Migration Guide Summary

[2-3 sentence summary of what changed and why]

### Affected Code Locations

**File: `[path/to/file.rs]`** ([N] occurrences)
```rust
// Context from surrounding code (3-5 lines)
[actual code snippet from your search]
// More context
```

[Repeat for each affected file, limit to top 10 files if more]

### Migration Instructions

1. [Specific step-by-step instructions for this codebase]
2. [Reference the official guide for details: see guide file above]
3. [Note any special considerations for this project]

### Search Pattern

To find all occurrences:
```bash
rg "pattern" --type rust
```

---
```

**Output Requirements:**
- Generate ONLY the markdown section above
- Include actual code snippets from ${CODEBASE}
- Use relative paths from ${CODEBASE}
- Limit to top 10 most important locations if there are many
- Be specific to THIS codebase, not generic
- Do NOT use line numbers (they become stale)
- Use 3-5 lines of context around each match
- **CRITICAL**: Start your response with "##" followed by the title - no preamble or commentary
- **CRITICAL**: Include the "**Guide File:**" line with full path to the guide
- **CRITICAL**: Include the "**Requirement Level:**" field - required for sorting
- **CRITICAL**: End with the triple-dash separator (---) - nothing after
</Pass2OutputTemplate>

---

<CloneRepository>

**Create TODO list:**

```
[
  {"content": "Validate Bevy version exists on GitHub", "status": "in_progress", "activeForm": "Validating Bevy version exists on GitHub"},
  {"content": "Clone Bevy repository (if needed)", "status": "pending", "activeForm": "Cloning Bevy repository"},
  {"content": "Check dependency compatibility", "status": "pending", "activeForm": "Checking dependency compatibility"},
  {"content": "Pass 1: Filter applicable guides (10 parallel subagents)", "status": "pending", "activeForm": "Pass 1: Filtering applicable guides"},
  {"content": "Pass 2: Deep analysis (N parallel subagents)", "status": "pending", "activeForm": "Pass 2: Deep analysis of applicable guides"},
  {"content": "Merge and present migration plan", "status": "pending", "activeForm": "Merging and presenting migration plan"}
]
```

**Validate version exists on GitHub:**

```bash
gh api repos/bevyengine/bevy/releases/tags/v${VERSION}
```

**If error (404 or other):**
- Show error message: "Error: Bevy version ${VERSION} not found on GitHub"
- Try to list available recent versions:
  ```bash
  gh api repos/bevyengine/bevy/releases --jq '.[0:10] | .[] | .tag_name'
  ```
- Show message: "Available recent versions: ..."
- Stop execution

<UpdateTodoProgress old_task="Validate Bevy version exists on GitHub" new_task="Clone Bevy repository (if needed)"/>

**Clone repository if needed:**

```bash
~/.claude/scripts/bevy_migration_ensure_repo.sh "${VERSION}"
```

**Verify migration guides exist:**

```bash
~/.claude/scripts/verify_migration_guides.sh "${GUIDES_DIR}"
```

**Create output directory:**

```bash
mkdir -p "${CODEBASE}/.claude/bevy_migration"
```

<UpdateTodoProgress old_task="Clone Bevy repository (if needed)" new_task="Check dependency compatibility"/>

</CloneRepository>

---

<DependencyCompatibilityCheck>

**Run dependency compatibility check and save output:**

```bash
DEPENDENCY_OUTPUT="/tmp/bevy_deps_${VERSION}.md"
~/.claude/scripts/bevy_migration_dependency_check.py --bevy-version "${VERSION}" --codebase "${CODEBASE}" --output "${DEPENDENCY_OUTPUT}"
```

**The script will:**
- Run `cargo tree` to discover all bevy-dependent crates (direct and indirect)
- Query crates.io for each dependency to find compatible versions
- Classify each dependency as:
  - **🚫 BLOCKER**: No compatible version exists - cannot migrate
  - **🔄 UPDATE_REQUIRED**: Compatible version exists - must update Cargo.toml
  - **⚠️ CHECK_NEEDED**: Compatibility unclear - needs manual testing
  - **✅ OK**: Already compatible
- Output markdown section with dependency compatibility review
- Includes clear explanations of each classification category
- Lists specific actions needed for each dependency

The output file will be read during MergeMigrationPlan step and inserted after the Summary section.

<UpdateTodoProgress old_task="Check dependency compatibility" new_task="Pass 1: Filter applicable guides (10 parallel subagents)"/>

</DependencyCompatibilityCheck>

---

<Pass1_ApplicabilityFilter>

**Launch 10 parallel subagents for quick applicability filtering:**

Use the Task tool to launch 10 general-purpose subagents **in a single message** (parallel execution).

**For each subagent (1-10):**

Create a Task tool call with description: `"Pass 1: Subagent ${N}"`

**Task prompt:**

```
Pass 1 Analysis - Subagent ${N} of 10:

You are analyzing Bevy ${VERSION} migration guides to determine which ones apply to this codebase.

**Get your assigned guides:**

Run this script to get your tranche of migration guide files:
```bash
~/.claude/scripts/bevy_migration_get_tranche.py \
  --guides-dir "${GUIDES_DIR}" \
  --subagent-index ${N}
```

This outputs JSON with "assigned_guides" array containing the guide file paths you should analyze.

**Your task:**

For EACH guide file in your assigned_guides:

1. Read the guide file from ${BEVY_REPO_DIR}/<path from assigned_guides>
2. Extract 3-5 key search patterns from the guide (types, functions, modules mentioned)
3. Run quick ripgrep searches in ${CODEBASE} for these patterns:
   ```bash
   rg "pattern" --type rust "${CODEBASE}"
   ```
4. Determine: APPLICABLE or NOT_APPLICABLE

**Output format (respond with ONLY this JSON):**

```json
{
  "applicable_files": [
    "release-content/migration-guides/some_guide.md",
    "release-content/migration-guides/another_guide.md"
  ],
  "not_applicable_files": [
    "release-content/migration-guides/irrelevant_guide.md"
  ]
}
```

**Rules:**
- Include file path in "applicable_files" array if you find ANY occurrences
- Include file path in "not_applicable_files" array if you find ZERO occurrences
- Do NOT include detailed analysis in Pass 1 - just presence/absence
- Do NOT read the entire codebase - use targeted ripgrep searches only
- Output MUST be valid JSON

**Working directory:** ${CODEBASE}
**Bevy repository:** ${BEVY_REPO_DIR}
```

**Wait for all 10 subagents to complete:**

**Extract applicable guide files:**

1. Parse JSON from all 10 subagent outputs:
   - Each subagent returns JSON with "applicable_files" and "not_applicable_files" arrays
   - Merge all "applicable_files" arrays into single list of unique file paths
   - Store this as APPLICABLE_GUIDE_FILES (list of file paths)

**Handle zero applicable guides edge case:**

If APPLICABLE_GUIDE_FILES is empty (no applicable guides found):
1. Skip Pass 2 entirely
2. Create minimal migration plan at ${MIGRATION_PLAN} stating no guides apply
3. Update TODO: mark "Pass 1" completed, mark "Pass 2" completed (skipped)
4. Jump to PresentResults with appropriate messaging

<UpdateTodoProgress old_task="Pass 1: Filter applicable guides (10 parallel subagents)" new_task="Pass 2: Deep analysis (N parallel subagents)"/>

</Pass1_ApplicabilityFilter>

---

<Pass2_DetailedAnalysis>

**Launch N parallel subagents (one per applicable guide):**

From Pass 1, you now have APPLICABLE_GUIDE_FILES - a list of guide file paths like:
- `release-content/migration-guides/some_guide.md`
- `release-content/migration-guides/another_guide.md`

**IMPORTANT:** Launch all subagents in a **single message** with multiple Task tool calls for maximum parallelism.

**For each guide file in APPLICABLE_GUIDE_FILES:**

Iterate through APPLICABLE_GUIDE_FILES. For each file path, create a Task tool call with:

**Task description parameter:**
```
"Deep Analysis: ${GUIDE_FILENAME} (${CURRENT_INDEX} of ${TOTAL_COUNT})"
```

Where:
- `${GUIDE_FILENAME}` is the basename of the guide file (e.g., "some_guide.md")
- `${CURRENT_INDEX}` is the 1-based position in APPLICABLE_GUIDE_FILES
- `${TOTAL_COUNT}` is the total number of applicable guides

**Task prompt parameter (substitute ${GUIDE_FILE_PATH} from current iteration):**

```
Deep Analysis Task for ${GUIDE_FILE_PATH}:

You are performing detailed migration analysis for a single Bevy ${VERSION} migration guide.

**Your guide:**
${BEVY_REPO_DIR}/${GUIDE_FILE_PATH}

**Your task:**

1. **Read the full guide** from ${BEVY_REPO_DIR}/${GUIDE_FILE_PATH}
2. **Extract ALL identifiers** mentioned in the guide:
   - Types, traits, functions, methods, modules
   - Old names (being removed/renamed)
   - New names (replacements)

3. **Search the codebase** for EVERY identifier:
   ```bash
   rg "identifier" --type rust -C 3 "${CODEBASE}"
   ```

4. **Classify requirement level:**
   - REQUIRED: Breaking changes that will cause compilation failures
   - HIGH: Deprecated features that still compile but need migration
   - MEDIUM: Optional improvements or new features
   - LOW: Minor changes or optimizations

5. Generate output using <Pass2OutputTemplate/> format
   - In the output, include a link to the guide file: ${BEVY_REPO_DIR}/${GUIDE_FILE_PATH}
   - Use the guide filename (without .md extension) as the title if no title is in the guide

**Working directory:** ${CODEBASE}
**Bevy repository:** ${BEVY_REPO_DIR}
**Guide file:** ${GUIDE_FILE_PATH}
```

**Wait for all N subagents to complete:**

Collect markdown sections from each subagent.

**Validate subagent outputs:**

For each section, verify it contains a parseable "**Requirement Level:**" field (REQUIRED/HIGH/MEDIUM/LOW). If a section is missing this field or has unparseable content, report an error message indicating which guide failed validation and skip that guide with a note in the final plan's "Issues During Analysis" section.

<UpdateTodoProgress old_task="Pass 2: Deep analysis (N parallel subagents)" new_task="Merge and present migration plan"/>

</Pass2_DetailedAnalysis>

---

<MergeMigrationPlan>

**Merge all subagent outputs into final migration plan:**

1. **Create output directory:**
   ```bash
   mkdir -p "${CODEBASE}/.claude/bevy_migration"
   ```

2. **Read dependency compatibility output:**

   Use Read tool to read ${DEPENDENCY_OUTPUT}.
   Store this content to insert after the Summary section.

3. **Sort sections by requirement level:**
   - REQUIRED guides first
   - Then HIGH
   - Then MEDIUM
   - Then LOW

4. **Generate final document structure:**

```markdown
# Bevy ${VERSION} Migration Plan

**Generated:** [timestamp]
**Codebase:** ${CODEBASE}
**Total Applicable Guides:** [N]

---

## Summary

- **REQUIRED changes:** [X] guides ([Y] total occurrences)
- **HIGH priority:** [X] guides ([Y] total occurrences)
- **MEDIUM priority:** [X] guides ([Y] total occurrences)
- **LOW priority:** [X] guides ([Y] total occurrences)

**Estimated effort:** [Based on occurrence counts]
- REQUIRED: [Large/Medium/Small] (must fix to compile)
- HIGH: [Large/Medium/Small] (should fix soon)
- MEDIUM: [Large/Medium/Small] (optional improvements)
- LOW: [Large/Medium/Small] (nice to have)

---

[Read and insert dependency output from ${DEPENDENCY_OUTPUT} here]

---

## REQUIRED Changes

[Sections from Pass 2 for REQUIRED guides - each section includes **Guide File:** link]

---

## HIGH Priority Changes

[Sections from Pass 2 for HIGH guides - each section includes **Guide File:** link]

---

## MEDIUM Priority Changes

[Sections from Pass 2 for MEDIUM guides - each section includes **Guide File:** link]

---

## LOW Priority Changes

[Sections from Pass 2 for LOW guides - each section includes **Guide File:** link]

---

## Guides Not Applicable to This Codebase

The following [X] guides from Bevy ${VERSION} do not apply to this codebase.

[List file paths from Pass 1 not_applicable_files arrays - no need to read the files, just list them]

---

## Next Steps

1. Start with REQUIRED changes (must fix to compile with Bevy ${VERSION})
2. Address HIGH priority changes (deprecated features)
3. Consider MEDIUM and LOW priority improvements
4. Test thoroughly after each category of changes
5. Run `cargo check` and `cargo test` frequently

---

## Reference

- **Migration guides directory:** ${GUIDES_DIR}
- **Bevy ${VERSION} release notes:** https://github.com/bevyengine/bevy/releases/tag/v${VERSION}
```

5. **Write final migration plan:**

   Use Write tool to create ${MIGRATION_PLAN} with the following structure:
   - Header with metadata (generated timestamp, codebase path, total guides)
   - Summary section with occurrence counts by priority level
   - Dependency compatibility section (content from ${DEPENDENCY_OUTPUT})
   - REQUIRED Changes section (Pass 2 sections with REQUIRED level - each includes **Guide File:** link)
   - HIGH Priority Changes section (Pass 2 sections with HIGH level - each includes **Guide File:** link)
   - MEDIUM Priority Changes section (Pass 2 sections with MEDIUM level - each includes **Guide File:** link)
   - LOW Priority Changes section (Pass 2 sections with LOW level - each includes **Guide File:** link)
   - Guides Not Applicable section (file paths from Pass 1 not_applicable_files - just list the paths)
   - Next Steps section
   - Reference section (link to ${GUIDES_DIR})

   Concatenate all sections in this order and write to ${MIGRATION_PLAN} using Write tool.

**Note:** The final TODO update happens in PresentResults after displaying the summary.

</MergeMigrationPlan>

---

<PresentResults>

**Present the final migration plan:**

Show a summary of what was generated:

```
═══════════════════════════════════════════════════════════
✓ Bevy ${VERSION} Migration Plan Generated
═══════════════════════════════════════════════════════════

Migration Plan:
  ${MIGRATION_PLAN}

Analysis Summary:
- Analyzed: 114 migration guides
- Applicable: [N] guides affect this codebase
- Not applicable: [114-N] guides

Breakdown by Priority:
- REQUIRED: [X] guides ([Y] occurrences) - MUST FIX
- HIGH: [X] guides ([Y] occurrences) - SHOULD FIX SOON
- MEDIUM: [X] guides ([Y] occurrences) - OPTIONAL
- LOW: [X] guides ([Y] occurrences) - NICE TO HAVE

Parallel Analysis:
✓ Pass 1: 10 subagents filtered applicable guides
✓ Pass 2: [N] subagents analyzed each guide in depth

Next Steps:
1. Review the migration plan: ${MIGRATION_PLAN}
2. Start with REQUIRED changes
3. Run cargo check frequently during migration
4. Test thoroughly after each category

The migration plan is specific to YOUR codebase with actual
code locations, snippets, and targeted instructions.
═══════════════════════════════════════════════════════════
```

Use TodoWrite tool to mark "Merge and present migration plan" as completed.

</PresentResults>

