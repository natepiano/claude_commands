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

<Persona>
@~/.claude/shared/personas/principal_engineer_persona.md

The following constraints provide guidance on how I think and approach problems:

@~/.claude/shared/constraints/code_review_constraints.md
</Persona>

---

<ExecutionSteps>

**EXECUTE THESE STEPS IN ORDER:**

**STEP 0:** Execute <Persona/> to adopt the Principal Engineer persona
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
**Pass 1 Count:** [Z] | **Pass 2 Count:** [X] | **Status:** [MATCH/ANOMALY: ±N%]

### Migration Guide Summary

[2-3 sentence summary of what changed and why]

### Required Changes

**1. Update [description] in `path/to/file.rs`**
```diff
- [old code]
+ [new code]
```

**2. Update [description] in `path/to/other_file.rs`**
```diff
- [old code]
+ [new code]
```

[Repeat for EVERY occurrence - do not limit to top 10]

### Search Pattern

To find all occurrences:
```bash
rg "pattern" --type rust
```

---
```

**Output Requirements:**
- Generate ONLY the markdown section above
- Show actual before/after diff for EVERY occurrence (no "top 10" limit)
- Use relative paths from ${CODEBASE}
- Use diff format with `-` for old code, `+` for new code
- Be specific to THIS codebase's actual code
- Each change should have:
  - Numbered entry (1, 2, 3, ...)
  - Brief description of what's being updated
  - File path
  - Concrete before/after diff from the actual codebase
- **CRITICAL**: Start your response with "##" followed by the title - no preamble or commentary
- **CRITICAL**: Include the "**Guide File:**" line with full path to the guide
- **CRITICAL**: Include the "**Requirement Level:**" field - required for sorting
- **CRITICAL**: End with the triple-dash separator (---) - nothing after
</Pass2OutputTemplate>

---

<ApplicableGuidesSummary>
Output a formatted list showing all applicable guides found in Pass 1:

```
## Pass 1: Applicable Guides Found

From the 10 subagent outputs, I found these applicable guides:
  1. [guide_name.md] ([total_occurrences] occurrences)
  2. [guide_name.md] ([total_occurrences] occurrences)
  ...

Launching Pass 2 deep analysis for [N] guides...
```

**Requirements:**
- Number each guide sequentially
- Show guide filename (basename only)
- Show Pass 1 total occurrence count in parentheses
- Sort by occurrence count (highest first)
</ApplicableGuidesSummary>

---

<CountingProcedure>

**Standard pattern counting - use this for all occurrence counts:**

Single pattern:
```bash
count=$(~/.claude/scripts/bevy_migration_count_pattern.sh "pattern" "${CODEBASE}" rust)
```

Multiple patterns with breakdown:
```bash
~/.claude/scripts/bevy_migration_count_pattern.sh --multiple "pattern1" "pattern2" "pattern3" -- "${CODEBASE}" rust
```

Verify Pass 2 counts against Pass 1 total (for Pass 2 validation):
```bash
~/.claude/scripts/bevy_migration_count_pattern.sh --verify \
  --pass1-total ${PASS1_TOTAL} \
  --patterns "pattern1" "pattern2" "pattern3" -- "${CODEBASE}" rust
```

The script outputs:
- Single mode: Just the number (e.g., `42`)
- Multiple mode: JSON with pattern breakdown and `_total` field
- Verify mode: JSON with `pass1_total`, `pass2_total`, `breakdown`, `variance_percent`, and `status` ("MATCH" or "ANOMALY")

**Do NOT write custom Python scripts, echo commands with nested substitutions, or manual counting logic - use this script.**

</CountingProcedure>

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
~/.claude/scripts/bevy_migration_verify_guides.sh "${GUIDES_DIR}"
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
~/.claude/scripts/bevy_migration_dependency_check.py --bevy-version "${VERSION}" --codebase "${CODEBASE}" --output "/tmp/bevy_migration_deps_$(basename ${CODEBASE}).md"
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

The output file will be `/tmp/bevy_migration_deps_$(basename ${CODEBASE}).md` and will be read during MergeMigrationPlan step and inserted after the Summary section.

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
3. Count occurrences for each pattern using <CountingProcedure/>:
   ```bash
   count=$(~/.claude/scripts/bevy_migration_count_pattern.sh "pattern" "${CODEBASE}" rust)
   ```
4. Determine: APPLICABLE or NOT_APPLICABLE

**Output format (respond with ONLY this JSON):**

```json
{
  "applicable_guides": [
    {
      "guide_path": "release-content/migration-guides/some_guide.md",
      "matched_patterns": [
        {"pattern": "SomeType", "occurrences": 15},
        {"pattern": "some_function", "occurrences": 8}
      ],
      "total_occurrences": 23
    }
  ],
  "not_applicable_guides": [
    {
      "guide_path": "release-content/migration-guides/irrelevant_guide.md",
      "reason": "No matches for any searched patterns"
    }
  ]
}
```

**Rules:**
- For APPLICABLE guides: Include guide_path, ALL patterns you searched, occurrence count for each, and total
- For NOT_APPLICABLE guides: Include guide_path and brief reason
- Track EVERY pattern you search for, even if it has 0 occurrences
- Do NOT include detailed analysis in Pass 1 - just pattern matching results
- Do NOT read the entire codebase - use targeted ripgrep searches only
- Output MUST be valid JSON with the exact structure shown above

**Working directory:** ${CODEBASE}
**Bevy repository:** ${BEVY_REPO_DIR}
```

**Wait for all 10 subagents to complete:**

**Extract applicable guide data:**

1. Parse JSON from all 10 subagent outputs:
   - Each subagent returns JSON with "applicable_guides" and "not_applicable_guides" arrays
   - Merge all "applicable_guides" arrays into single list
   - Store this as APPLICABLE_GUIDES_DATA (list of objects with guide_path, matched_patterns, total_occurrences)

2. **Output applicable guides summary using <ApplicableGuidesSummary/>:**
   - Sort APPLICABLE_GUIDES_DATA by total_occurrences (highest first)
   - Display formatted list with guide names and occurrence counts
   - This provides visibility into what Pass 2 will analyze

**Handle zero applicable guides edge case:**

If APPLICABLE_GUIDES_DATA is empty (no applicable guides found):
1. Skip Pass 2 entirely
2. Create minimal migration plan at ${MIGRATION_PLAN} stating no guides apply
3. Update TODO: mark "Pass 1" completed, mark "Pass 2" completed (skipped)
4. Jump to PresentResults with appropriate messaging

<UpdateTodoProgress old_task="Pass 1: Filter applicable guides (10 parallel subagents)" new_task="Pass 2: Deep analysis (N parallel subagents)"/>

</Pass1_ApplicabilityFilter>

---

<Pass2_DetailedAnalysis>

**Launch N parallel subagents (one per applicable guide):**

From Pass 1, you now have APPLICABLE_GUIDES_DATA - a list of objects containing:
- `guide_path`: Path to the guide file
- `matched_patterns`: Array of {pattern, occurrences} objects showing what matched in Pass 1
- `total_occurrences`: Total occurrence count from Pass 1

**IMPORTANT:** Launch all subagents in a **single message** with multiple Task tool calls for maximum parallelism.

**For each guide in APPLICABLE_GUIDES_DATA:**

Iterate through APPLICABLE_GUIDES_DATA. For each guide object, create a Task tool call with:

**Task description parameter:**
```
"Deep Analysis: ${GUIDE_FILENAME} (${CURRENT_INDEX} of ${TOTAL_COUNT})"
```

Where:
- `${GUIDE_FILENAME}` is the basename of the guide file (e.g., "some_guide.md")
- `${CURRENT_INDEX}` is the 1-based position in APPLICABLE_GUIDES_DATA
- `${TOTAL_COUNT}` is the total number of applicable guides

**Task prompt parameter (substitute values from current guide object):**

```
Deep Analysis Task for ${GUIDE_FILE_PATH}:

You are performing detailed migration analysis for a single Bevy ${VERSION} migration guide.

**Your guide:**
${BEVY_REPO_DIR}/${GUIDE_FILE_PATH}

**Pass 1 Results (use these patterns):**
Matched patterns from Pass 1:
${MATCHED_PATTERNS_JSON}

Expected total occurrences from Pass 1: ${PASS1_TOTAL_OCCURRENCES}

**Your task:**

1. **Read the full guide** from ${BEVY_REPO_DIR}/${GUIDE_FILE_PATH}

2. **Verify understanding of technical terminology:**
   - If the guide uses domain-specific terms (e.g., "generic types", "observers", "messages"), look for:
     - Concrete code examples in the guide showing what qualifies
     - Before/after snippets that clarify the meaning
     - Explicit definitions or exclusions
   - When searching the codebase, verify your matches look like the guide's examples
   - If uncertain about a term's meaning:
     - Note your interpretation in the output
     - Include a caveat if matches might need manual review
     - Err on the side of conservative interpretation

   **Example:** If guide says "generic types must still be registered":
   - Check guide for examples of what "generic types" means in this context
   - Look for code snippets showing `Foo<T>` vs `Foo { field: HashMap<K,V> }`
   - Don't assume - verify against the guide's own examples

3. **Inspect code with Pass 1 patterns (for writing diffs):**
   - DO NOT re-extract patterns from the guide
   - Use the EXACT patterns provided in the matched_patterns list above
   - Use rg with context to inspect actual code (needed for writing before/after diffs):
   ```bash
   rg "pattern_from_pass1" --type rust -C 3 "${CODEBASE}"
   ```
   - **DO NOT count manually** - counting happens in step 4
   - This step is ONLY for inspecting code context to write accurate diffs

4. **Count ALL patterns and validate (single step):**

   **STEP A: Construct and declare your exact command**

   First, build your command using this template:
   ```
   ~/.claude/scripts/bevy_migration_count_pattern.sh --verify --pass1-total [N] --patterns "p1" "p2" "p3" -- "${CODEBASE}" rust
   ```

   Then output this declaration with your EXACT command:
   ```
   I will now run this exact command with ZERO modifications:
   ~/.claude/scripts/bevy_migration_count_pattern.sh --verify --pass1-total [actual number] --patterns [actual patterns] -- [actual path] rust
   ```

   **STEP B: Run the command and immediately describe what you see**

   Copy the EXACT command from Step A and run it. IMMEDIATELY after the Bash tool runs, describe the output you see:

   "The Bash tool shows me this JSON output:
   ```json
   {
     "pass1_total": [number],
     "breakdown": {
       "pattern1": [count],
       "pattern2": [count]
     },
     "pass2_total": [number],
     "variance_percent": [number],
     "status": "[MATCH or ANOMALY]"
   }
   ```"

   You must describe what you SEE in the Bash tool output - do not save to files or redirect.
   If you save to a file, you cannot describe what you see, so just run the command directly.

   **STEP C: Use the output in your analysis**
   - `pass2_total`: Your total occurrence count
   - `breakdown`: Individual pattern counts
   - `status`: "MATCH" (≤20% variance) or "ANOMALY" (>20% variance)
   - Include variance explanation if status is "ANOMALY"
   - If pass2_total is 0 but Pass 1 found ${PASS1_TOTAL_OCCURRENCES}, STOP and report error

5. **Classify requirement level:**
   - REQUIRED: Breaking changes that will cause compilation failures
   - HIGH: Deprecated features that still compile but need migration
   - MEDIUM: Optional improvements or new features
   - LOW: Minor changes or optimizations
   - **If 0 occurrences found:** Classify as LOW (informational only)

6. Generate output using <Pass2OutputTemplate/> format
   - In the output, include a link to the guide file: ${BEVY_REPO_DIR}/${GUIDE_FILE_PATH}
   - Use the guide filename (without .md extension) as the title if no title is in the guide
   - **CRITICAL**: Report occurrence counts:
     - Pass 1 Count: ${PASS1_TOTAL_OCCURRENCES}
     - Pass 2 Count: [your actual count]
     - Status: "MATCH" if within 20%, otherwise "ANOMALY: ±X%"

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

   Use Read tool to read `/tmp/bevy_migration_deps_$(basename ${CODEBASE}).md`.
   Store this content as DEPENDENCY_OUTPUT to insert after the Summary section.

   **Important:** After reading, delete the temp file to prevent stale data in future runs:
   ```bash
   rm -f "/tmp/bevy_migration_deps_$(basename ${CODEBASE}).md"
   ```

3. **Sort sections by requirement level and identify anomalies:**
   - REQUIRED guides first, then HIGH, then MEDIUM, then LOW
   - For each section, parse the occurrence counts from the header:
     - Extract Pass 1 Count and Pass 2 Count values
     - Check Status field for "ANOMALY" markers
   - Track any sections marked with "ANOMALY" status
   - Aggregate list of anomalies for Summary section

4. **Collect literal Pass 2 subagent outputs - NO SUMMARIZATION:**

   **CRITICAL CONSTRAINTS:**
   - Take the EXACT markdown output from EACH Pass 2 subagent response
   - DO NOT summarize, abbreviate, or truncate any subagent output
   - DO NOT substitute references to guide files instead of actual content
   - DO NOT omit diff blocks due to "length concerns"
   - MUST include EVERY numbered change, EVERY diff block, EVERY code snippet from every subagent
   - If a guide has 50 occurrences, ALL 50 must be listed with their specific diffs

   **Collection process:**
   - For each Pass 2 subagent response, extract the complete markdown section starting with "##" and ending with "---"
   - Verify each section contains the full <Pass2OutputTemplate/> structure
   - Group sections by their **Requirement Level:** field (REQUIRED, HIGH, MEDIUM, LOW)
   - Within each group, maintain the sections in their original order

   **Validation:**
   - Count total numbered changes across all sections
   - Verify this matches expected occurrence counts from Summary
   - If any section appears truncated or summarized, REJECT it and report error

4a. **Extract anomaly explanations for detailed reporting:**

   For each guide marked with ANOMALY status (>20% variance):
   - Extract the guide filename (basename only, e.g., "observer_and_event_changes.md")
   - Extract Pass 1 count, Pass 2 count, and variance percentage
   - Look for explanatory text in the Pass 2 subagent output explaining the discrepancy
   - Common explanations to look for:
     - "Pass 1 false positives from..."
     - "Pass 1 counted generic/common terms..."
     - "Pass 2 found additional..."
     - Context about what patterns matched incorrectly in Pass 1
   - Store these as ANOMALY_DETAILS (list of objects with: guide_name, pass1_count, pass2_count, variance_percent, explanation)
   - If no anomalies detected (empty list), this will be used to conditionally omit the Anomaly Analysis section

5. **Generate final document structure:**

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

**Count Anomalies:** [N] guides with >20% variance between Pass 1 and Pass 2
[If N > 0, list them: "- guide_name.md: Pass 1=[X], Pass 2=[Y] (±Z%)"]

**Estimated effort:** [Based on occurrence counts]
- REQUIRED: [Large/Medium/Small] (must fix to compile)
- HIGH: [Large/Medium/Small] (should fix soon)
- MEDIUM: [Large/Medium/Small] (optional improvements)
- LOW: [Large/Medium/Small] (nice to have)

---

[If anomalies detected (N > 0), insert the following section:]

## 🔍 Anomaly Analysis

During the two-pass analysis, [N] guide(s) showed significant variance (>20%) between initial pattern matching and deep contextual analysis:

[For each anomaly, include:]

### [guide_name.md]
- **Pass 1 Count:** [X] occurrences
- **Pass 2 Count:** [Y] occurrences
- **Variance:** ±[Z]%
- **Explanation:** [Detailed explanation of why the discrepancy occurred - what Pass 1 patterns matched that weren't actually relevant, or what Pass 2 found that Pass 1 missed]

[End conditional section - omit entire "## 🔍 Anomaly Analysis" section if N = 0]

---

[Insert DEPENDENCY_OUTPUT content here]

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

[List file paths from Pass 1 not_applicable_guides arrays - no need to read the files, just list them]

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

6. **Write final migration plan:**

   Use Write tool to create ${MIGRATION_PLAN} with the following structure:
   - Header with metadata (generated timestamp, codebase path, total guides)
   - Summary section with occurrence counts by priority level
   - Anomaly Analysis section (ONLY if ANOMALY_DETAILS is non-empty - include detailed explanations for each anomaly)
   - Dependency compatibility section (content from ${DEPENDENCY_OUTPUT})
   - REQUIRED Changes section (LITERAL COMPLETE Pass 2 markdown sections - ALL diffs, ALL changes)
   - HIGH Priority Changes section (LITERAL COMPLETE Pass 2 markdown sections - ALL diffs, ALL changes)
   - MEDIUM Priority Changes section (LITERAL COMPLETE Pass 2 markdown sections - ALL diffs, ALL changes)
   - LOW Priority Changes section (LITERAL COMPLETE Pass 2 markdown sections - ALL diffs, ALL changes)
   - Guides Not Applicable section (file paths from Pass 1 not_applicable_guides - just list the paths)
   - Next Steps section
   - Reference section (link to ${GUIDES_DIR})

   **Conditional Logic:**
   - If ANOMALY_DETAILS is empty, skip the entire "## 🔍 Anomaly Analysis" section
   - If ANOMALY_DETAILS has entries, generate the section with one subsection per anomaly

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

