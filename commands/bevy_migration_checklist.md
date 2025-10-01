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
**STEP 3:** Execute <DisplayExecutionPlan/>
**STEP 4:** Execute <CloneBevyRepo/>
**STEP 5:** Execute <Pass1_BasicExtraction/>
**STEP 6:** Execute <Pass2_SemanticReview/>
**STEP 7:** Execute <Pass3_MergeFindings/>
**STEP 8:** Execute <PresentResults/>

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
OUTPUT_DIR = ~/.claude/bevy_migration
PASS1_MARKDOWN = ~/.claude/bevy_migration/bevy-${VERSION}-checklist-pass1.md
PASS1_JSON = ~/.claude/bevy_migration/bevy-${VERSION}-checklist-pass1.json
AGENT_FINDINGS_DIR = ~/.claude/bevy_migration/agent-findings
FINAL_OUTPUT = ~/.claude/bevy_migration/bevy-${VERSION}-checklist-final.md
```

**Example:** If VERSION is "0.17.0", then when you see `${VERSION}` in bash commands, substitute it with "0.17.0". The command `gh api repos/bevyengine/bevy/releases/tags/v${VERSION}` becomes `gh api repos/bevyengine/bevy/releases/tags/v0.17.0`.

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

Three-Pass Processing:
  Pass 1: Python extraction of structure and content
  Pass 2: 10 parallel agents for semantic review
  Pass 3: Sequential merge into final checklist

File Outputs:
  Pass 1 Markdown: ~/.claude/bevy_migration/bevy-${VERSION}-checklist-pass1.md
  Pass 1 JSON:     ~/.claude/bevy_migration/bevy-${VERSION}-checklist-pass1.json
  Agent Findings:  ~/.claude/bevy_migration/agent-findings/agent-{01..10}-findings.md
  Final Checklist: ~/.claude/bevy_migration/bevy-${VERSION}-checklist-final.md

Execution Steps:
  1. Remove existing directory (if present)
     → rm -rf ~/rust/bevy-${VERSION}

  2. Clone Bevy repository
     → git clone https://github.com/bevyengine/bevy.git ~/rust/bevy-${VERSION}

  3. Checkout version tag v${VERSION}
     → git -C ~/rust/bevy-${VERSION} checkout v${VERSION}

  4. PASS 1: Basic extraction (Python)
     → python3 ~/.claude/scripts/bevy_migration_checklist_generator.py

  5. PASS 2: Semantic review (10 parallel agents)
     → Launch agents to enhance actionability

  6. PASS 3: Merge findings (Sequential)
     → python3 ~/.claude/scripts/bevy_migration_merge_findings.py

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
   if [ ! -d "${GUIDES_DIR}" ]; then
     echo "Error: Migration guides directory not found at ${GUIDES_DIR}"
     echo "The Bevy ${VERSION} release may not include migration guides, or the repository structure may have changed."
     exit 1
   fi

   if [ -z "$(ls -A "${GUIDES_DIR}" 2>/dev/null)" ]; then
     echo "Error: Migration guides directory is empty at ${GUIDES_DIR}"
     echo "The Bevy ${VERSION} release may not include migration guides."
     exit 1
   fi

   echo "Found $(ls "${GUIDES_DIR}" | wc -l) migration guide(s)"
   ```

**Progress messages:**
- "Cloning Bevy repository..."
- "Checking out version v${VERSION}..."
- "Verifying migration guides..."

</CloneBevyRepo>

---

<Pass1_BasicExtraction>

**Run Pass 1: Basic extraction with Python**

1. **Create output directory:**
   ```bash
   mkdir -p ${OUTPUT_DIR}
   ```

2. **Run the generator:**
   ```bash
   python3 ~/.claude/scripts/bevy_migration_checklist_generator.py \
     --version ${VERSION} \
     --guides-dir ${GUIDES_DIR} \
     --output ${PASS1_MARKDOWN} \
     --json-output ${PASS1_JSON}
   ```

**The script will:**
- Parse all `*.md` files in the migration-guides directory
- Extract titles, descriptions, bullet points, code blocks
- Generate search patterns from code and text
- Output basic markdown checklist AND structured JSON

**Output files:**
- `${PASS1_MARKDOWN}` - Basic checklist for review
- `${PASS1_JSON}` - Structured data for Pass 2

**Progress messages:**
- "Parsing X migration guides..."
- "Parsed X migration guides"
- "✓ Basic checklist generated"
- "✓ JSON data generated"

</Pass1_BasicExtraction>

---

<Pass2_SemanticReview>

**Run Pass 2: Semantic review with 10 parallel agents**

1. **Create agent findings directory:**
   ```bash
   mkdir -p ${AGENT_FINDINGS_DIR}
   ```

2. **Generate agent prompts:**
   ```bash
   python3 ~/.claude/scripts/bevy_migration_semantic_review.py \
     --json-input ${PASS1_JSON} \
     --output-dir ${AGENT_FINDINGS_DIR} \
     --num-agents 10
   ```

   This script:
   - Divides guides into 10 balanced batches
   - Creates prompts for each agent
   - Saves prompts to `${AGENT_FINDINGS_DIR}/prompts/`

3. **Launch 10 agents in parallel using Task tool:**

   Read the 10 agent prompts from `${AGENT_FINDINGS_DIR}/prompts/agent-{01..10}-prompt.txt`

   Use a **single message with 10 Task tool calls** to launch all agents in parallel:

   ```python
   # Pseudo-code for parallel launch
   for agent_id in 1..10:
       Task(
           description=f"Semantic review agent {agent_id}",
           subagent_type="general-purpose",
           prompt=read_file(f"${AGENT_FINDINGS_DIR}/prompts/agent-{agent_id:02d}-prompt.txt")
       )
   ```

   Each agent will write enhanced findings to:
   - `${AGENT_FINDINGS_DIR}/agent-01-findings.md`
   - `${AGENT_FINDINGS_DIR}/agent-02-findings.md`
   - ... through agent-10-findings.md

**What agents do:**
- Break complex changes into granular checklist items
- Extract concrete search patterns
- Identify required vs optional changes
- Clarify ambiguous descriptions
- Preserve all code examples
- Add helpful context

**Wait for all agents to complete before proceeding to Pass 3.**

**Progress messages:**
- "Loaded X migration guides for Bevy ${VERSION}"
- "Divided into 10 batches"
- "✓ Generated 10 agent prompts"
- "Launching 10 parallel agents..."
- "✓ All agents completed"

</Pass2_SemanticReview>

---

<Pass3_MergeFindings>

**Run Pass 3: Merge findings sequentially**

1. **Run the merge script:**
   ```bash
   python3 ~/.claude/scripts/bevy_migration_merge_findings.py \
     --agent-findings-dir ${AGENT_FINDINGS_DIR} \
     --output ${FINAL_OUTPUT} \
     --version ${VERSION} \
     --num-agents 10
   ```

**The script will:**
- Read all 10 agent finding files sequentially
- Parse enhanced guide sections
- Merge into final checklist preserving guide order
- Add header with processing statistics
- Write final enhanced checklist

**Output:**
- `${FINAL_OUTPUT}` - Final enhanced migration checklist

**Progress messages:**
- "Merging findings from 10 agents..."
- "✓ Agent 01: X guides"
- "✓ Agent 02: X guides"
- ... through agent 10
- "✓ Final checklist generated"

</Pass3_MergeFindings>

---

<PresentResults>

**Present the results to the user:**

Show a summary of what was generated:

```
═══════════════════════════════════════════════════════════
✓ Bevy ${VERSION} Migration Checklist Generated
═══════════════════════════════════════════════════════════

Final Enhanced Checklist:
  ${FINAL_OUTPUT}

Intermediate Files:
  Pass 1 Basic:    ${PASS1_MARKDOWN}
  Pass 1 JSON:     ${PASS1_JSON}
  Agent Findings:  ${AGENT_FINDINGS_DIR}/agent-{01..10}-findings.md

Processing Summary:
- Pass 1: Parsed X migration guides
- Pass 2: 10 agents enhanced Y checklist items
- Pass 3: Merged into comprehensive final checklist

Features:
✓ Granular, actionable checklist items
✓ Concrete search patterns for your code
✓ Required vs optional changes identified
✓ Official code examples preserved
✓ Clear migration guidance

Next Steps:
1. Review the final checklist: ${FINAL_OUTPUT}
2. Run /bevy_migration_plan ${VERSION} to generate a project-specific plan

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

- The checklist is generated from official Bevy migration guides
- It's saved globally in `~/.claude/bevy_migration/` for reuse across projects
- The Bevy repo is cloned to `~/rust/bevy-{version}/` for reference
- Run this command once per Bevy version
- After generating the checklist, use `/bevy_migration_plan` to create project-specific plans
