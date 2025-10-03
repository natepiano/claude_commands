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

## Execution Strategy

**Two-Pass Parallel Subagent Analysis:**

1. **Pass 1 - Quick Applicability Filter**: 10 subagents analyze 11-12 guides each to identify which guides apply to this codebase (fast pattern matching)
2. **Pass 2 - Deep Analysis**: N subagents (one per applicable guide) perform detailed code analysis and generate migration instructions

---

<ExecutionSteps>

**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <ParseArguments/>
**STEP 2:** Execute <GenerateCombinedGuides/>
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
COMBINED_GUIDES = ${CODEBASE}/.claude/bevy_migration/bevy-${VERSION}-guides.md
MIGRATION_PLAN = ${CODEBASE}/.claude/bevy_migration/bevy-${VERSION}-migration-plan.md
```

**Path Expansion Note:**
- When constructing bash commands, ensure tildes are expanded to actual paths
- If CODEBASE contains tilde (e.g., user provides "~/rust/my_game"), expand it before using in bash commands
- Use `${HOME}` instead of `~` in all template definitions to avoid ambiguity
- BEVY_REPO_DIR already uses `${HOME}` for reliable path resolution across all contexts

**Note:** The Bevy repository is cloned to a global location (`${HOME}/rust/bevy-${VERSION}`) to avoid duplicate clones, but the output files (combined guides and migration plan) are saved in the target project's `.claude/bevy_migration/` directory.

</ParseArguments>

---

<GenerateCombinedGuides>

**Create TODO list:**

```
[
  {"content": "Validate Bevy version exists on GitHub", "status": "in_progress", "activeForm": "Validating Bevy version exists on GitHub"},
  {"content": "Clone Bevy repository (if needed)", "status": "pending", "activeForm": "Cloning Bevy repository"},
  {"content": "Generate combined migration guides file", "status": "pending", "activeForm": "Generating combined migration guides file"},
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

**Mark TODO as completed and next as in_progress**

**Clone repository if needed:**

```bash
if [ -d "${BEVY_REPO_DIR}/.git" ]; then
  echo "Repository already exists at ${BEVY_REPO_DIR}"
  git -C "${BEVY_REPO_DIR}" checkout "v${VERSION}"
else
  echo "Cloning Bevy ${VERSION} to ${BEVY_REPO_DIR}"
  rm -rf "${BEVY_REPO_DIR}"  # Clean any partial/corrupt directories
  mkdir -p "$(dirname "${BEVY_REPO_DIR}")"
  git clone https://github.com/bevyengine/bevy.git "${BEVY_REPO_DIR}"
  git -C "${BEVY_REPO_DIR}" checkout "v${VERSION}"
fi
```

**Verify migration guides exist:**

```bash
~/.claude/scripts/verify_migration_guides.sh "${GUIDES_DIR}"
```

**Mark TODO as completed and next as in_progress**

**Generate combined guides file:**

```bash
mkdir -p "${CODEBASE}/.claude/bevy_migration"
echo "Generating combined guides file..."
~/.claude/scripts/bevy_migration_combine_guides.py "${VERSION}" "${GUIDES_DIR}" "${COMBINED_GUIDES}"
```

**The script outputs:**
- Combined markdown file with all 114 migration guides
- Table of Contents with 10 subagent sections
- Each guide marked with its line number for easy reference

**Verify the file was created:**
```bash
ls -lh "${COMBINED_GUIDES}"
```

**Mark TODO as completed and next as in_progress**

</GenerateCombinedGuides>

---

<DependencyCompatibilityCheck>

**Run dependency compatibility check:**

```bash
~/.claude/scripts/bevy_dependency_check.py --bevy-version "${VERSION}" --codebase "${CODEBASE}"
```

**The script will:**
- Run `cargo tree` to discover all bevy-dependent crates (direct and indirect)
- Query crates.io for each dependency to find compatible versions
- Classify each dependency as:
  - **🚫 BLOCKER**: No compatible version exists - cannot migrate
  - **🔄 UPDATE_REQUIRED**: Compatible version exists - must update Cargo.toml
  - **⚠️ CHECK_NEEDED**: Compatibility unclear - needs manual testing
  - **✅ OK**: Already compatible

**Output:**
- Markdown section with dependency compatibility review
- Includes clear explanations of each classification category
- Lists specific actions needed for each dependency

**Capture the output:**

Store the script's stdout output in a variable for later merging into the final migration plan. This section will appear immediately after the Summary section and before the REQUIRED Changes section.

**Mark TODO as completed and next as in_progress**

</DependencyCompatibilityCheck>

---

<Pass1_ApplicabilityFilter>

**Launch 10 parallel subagents for quick applicability filtering:**

Use the Task tool to launch 10 general-purpose subagents **in a single message** (parallel execution).

**For each subagent (1-10):**

```
Subagent ${N} Task:

You are analyzing Bevy ${VERSION} migration guides to determine which ones apply to this codebase.

**Your assigned guides:**
Read the "Subagent ${N}" section from the Table of Contents in ${COMBINED_GUIDES}. This lists your assigned migration guide numbers and their line numbers.

**Your task:**

For EACH of your assigned guides:

1. Read the guide at the specified line number in ${COMBINED_GUIDES}
2. Extract 3-5 key search patterns from the guide (types, functions, modules mentioned)
3. Run quick ripgrep searches in ${CODEBASE} for these patterns:
   ```bash
   rg "pattern" --type rust "${CODEBASE}"
   ```
4. Determine: APPLICABLE or NOT_APPLICABLE

**Output format (respond with ONLY this structured list):**

```
APPLICABLE_GUIDES:
- Guide N: [title] - Found [X] occurrences of [key_pattern]
- Guide M: [title] - Found [Y] occurrences of [key_pattern]

NOT_APPLICABLE_GUIDES:
- Guide K: [title] - No matches found
- Guide L: [title] - No matches found
```

**Rules:**
- Mark as APPLICABLE if you find ANY occurrences of relevant patterns
- Mark as NOT_APPLICABLE only if you find ZERO occurrences
- Do NOT include detailed analysis in Pass 1 - just presence/absence
- Do NOT read the entire codebase - use targeted ripgrep searches only
- Your response should be concise (1-2 lines per guide)

**Working directory:** ${CODEBASE}
```

**Wait for all 10 subagents to complete:**

Collect results from all subagents. Parse the APPLICABLE_GUIDES lists to build a master list of applicable guide numbers.

**Mark TODO as completed and next as in_progress**

</Pass1_ApplicabilityFilter>

---

<Pass2_DetailedAnalysis>

**Launch N parallel subagents (one per applicable guide):**

From Pass 1, you now have a list of applicable guide numbers. For each applicable guide, launch ONE general-purpose subagent to perform deep analysis.

**IMPORTANT:** Launch all subagents in a **single message** with multiple Task tool calls for maximum parallelism.

**For each applicable guide:**

```
Deep Analysis Task for Guide ${GUIDE_NUM}:

You are performing detailed migration analysis for a single Bevy ${VERSION} migration guide.

**Your guide:**
Guide ${GUIDE_NUM} in ${COMBINED_GUIDES} at line ${LINE_NUM}

**Your task:**

1. **Read the full guide** from ${COMBINED_GUIDES}
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

5. **Generate markdown section** with this EXACT structure:

```markdown
## Guide ${GUIDE_NUM}: [Title from guide]

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
2. [Reference the official guide for details]
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
- **CRITICAL**: Start your response with "## Guide" - no preamble or commentary
- **CRITICAL**: Include the "**Requirement Level:**" field - required for sorting
- **CRITICAL**: End with the triple-dash separator (---) - nothing after

**Working directory:** ${CODEBASE}
**Combined guides:** ${COMBINED_GUIDES}
```

**Wait for all N subagents to complete:**

Collect markdown sections from each subagent.

**Validate subagent outputs:**

For each section, verify it contains a parseable "**Requirement Level:**" field (REQUIRED/HIGH/MEDIUM/LOW). If a section is missing this field or has unparseable content, report an error message indicating which guide failed validation and skip that guide with a note in the final plan's "Issues During Analysis" section.

**Mark TODO as completed and next as in_progress**

</Pass2_DetailedAnalysis>

---

<MergeMigrationPlan>

**Merge all subagent outputs into final migration plan:**

1. **Create output directory:**
   ```bash
   mkdir -p "${CODEBASE}/.claude/bevy_migration"
   ```

2. **Sort sections by requirement level:**
   - REQUIRED guides first
   - Then HIGH
   - Then MEDIUM
   - Then LOW

3. **Generate final document structure:**

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

[INSERT DEPENDENCY COMPATIBILITY CHECK OUTPUT HERE]

---

## REQUIRED Changes

[Sections from Pass 2 for REQUIRED guides]

---

## HIGH Priority Changes

[Sections from Pass 2 for HIGH guides]

---

## MEDIUM Priority Changes

[Sections from Pass 2 for MEDIUM guides]

---

## LOW Priority Changes

[Sections from Pass 2 for LOW guides]

---

## Guides Not Applicable to This Codebase

The following [X] guides from Bevy ${VERSION} do not apply to this codebase:

- Guide N: [title]
- Guide M: [title]
[etc.]

---

## Next Steps

1. Start with REQUIRED changes (must fix to compile with Bevy ${VERSION})
2. Address HIGH priority changes (deprecated features)
3. Consider MEDIUM and LOW priority improvements
4. Test thoroughly after each category of changes
5. Run `cargo check` and `cargo test` frequently

---

## Reference

- **Official guides:** ${COMBINED_GUIDES}
- **Bevy ${VERSION} release notes:** https://github.com/bevyengine/bevy/releases/tag/v${VERSION}
```

4. **Write final migration plan:**
   ```bash
   # Write the merged content to ${MIGRATION_PLAN}
   ```

**Mark TODO as completed and next as in_progress**

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

**Mark final TODO as completed**

</PresentResults>

---

## What Gets Generated

The migration plan includes for each applicable guide:

- **Requirement Level**: REQUIRED/HIGH/MEDIUM/LOW
- **Occurrence Count**: How many places in YOUR code are affected
- **Migration Summary**: What changed and why
- **Affected Locations**: Actual code snippets from YOUR codebase with context
- **Migration Instructions**: Step-by-step guide specific to YOUR code
- **Search Patterns**: ripgrep commands to find all occurrences

---

## Key Features

✅ **Two-pass parallel analysis** - Fast filtering then deep analysis
✅ **Project-specific** - Analyzes YOUR actual codebase
✅ **Prioritized** - REQUIRED → HIGH → MEDIUM → LOW
✅ **Code snippets** - Real code from your project with context
✅ **No stale line numbers** - Uses greppable context instead
✅ **Scalable** - Handles 100+ guides efficiently
✅ **Fault-tolerant** - Each guide analyzed independently
✅ **Progress visible** - TODO list tracks each step

---

## Notes

- The Bevy repo is cloned globally to `~/rust/bevy-{version}/` (reusable across all projects)
- Both the combined guides file and migration plan are saved to the target project's `.claude/bevy_migration/` directory
- Pass 1 is fast (simple pattern matching across 10 subagents)
- Pass 2 is thorough (one subagent per applicable guide does deep analysis)
- Run this command once per Bevy version per project
- Re-running overwrites both files (useful for testing or as you make progress)
- Can analyze different projects by specifying the path argument
- Useful for comparing migration needs across multiple Bevy projects
