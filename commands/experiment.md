# experiment

**Purpose:** Guide structured experimentation for bug fixes and feature implementations with automatic documentation to plan files. Designed for MCP development workflows requiring server reloads.

**Usage:** `/experiment [plan_path] [mode]`

**Arguments:**
- `plan_path` (optional): Path to plan document (defaults to `.claude/plans/` search)
- `mode` (optional): `propose` | `document` (defaults to `propose`)

**Modes:**
- `propose`: Start new experiment (Steps 0-3: propose → document → implement → install)
- `document`: Document results after testing (Step 4: test and update plan)

<Persona>
@~/.claude/shared/personas/principal_engineer_persona.md

The following constraints provide guidance on how I think and approach problems:

@~/.claude/shared/constraints/code_review_constraints.md
</Persona>

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 0:** Execute <Persona/> to adopt the Principal Engineer persona

**If mode is `propose` (or default):**
1. Execute <LoadPlanDocument/>
2. Execute <ProposeCodeWithContext/>
3. Execute <UserApprovalProposal/>
4. If approved: Execute <DocumentExperimentToPlan/>
5. Execute <ImplementChange/>
6. Execute <CompleteImplementationAndStop/>

**If mode is `document`:**
1. Execute <LoadPlanDocument/>
2. Execute <TestAndDocumentResults/>
3. Execute <UpdateExperimentEntry/>
</ExecutionSteps>

---

<LoadPlanDocument>
**Goal:** Locate and read the plan document

**DECISION:** Context-based plan detection when no arguments, explicit path when provided

**Steps:**
1. If $ARGUMENTS provided: Use that path as the plan document
2. Else (no arguments): Analyze current conversation context
   - Identify what's being discussed (bug, feature, implementation)
   - Look for references to existing plan files in context
   - If context suggests an existing plan file, use that
   - If no plan file evident:
     - Generate suggested filename based on discussion topic (e.g., `fix-grandchildren-filtering.md`, `implement-depth-tracking.md`)
     - Default location: `.claude/plans/`
     - Ask user: "I'll create a plan document at `.claude/plans/[suggested-name].md`. Use this name, or provide a different path?"
     - Wait for user confirmation or alternative path
     - Do NOT create file yet (will be created when documenting experiment)
3. Read the plan document (if it exists)
4. Identify the "Experiment History" section
   - Search for heading: `## Experiment History`
   - If found: Note location for later experiment insertion
5. If no "Experiment History" section exists:
   - Ask user: "No Experiment History section found. Should I create one?"
   - If yes: Execute <CreateExperimentHistorySection/>
   - If no: Stop (cannot proceed without Experiment History)
6. Store plan path for later updates
</LoadPlanDocument>

---

<CreateExperimentHistorySection>
**Goal:** Create a properly formatted Experiment History section in the plan document

**Structure to create:**

```markdown
## Experiment History

This section documents all experimental attempts to solve the problem, following the scientific method of hypothesis, implementation, and result analysis.

### Attempt 1: [Brief Description] ([Date])

**Hypothesis:** [One sentence describing what you think will work and why]

**Analysis:**
[Understanding of the current problem - can be multiple paragraphs]
- Why previous approaches didn't work (if applicable)
- Key insight that motivates this approach
- What makes this attempt different

**Change Location:** [file_path]:[line_numbers]

**What we're changing:**
[Explanation of the changes in plain English]

**Code:**
\`\`\`[language]
[Code snippets showing the changes]
\`\`\`

**Expected outcome:**
[Specific, measurable expectations]
- What should change in behavior
- What values/output to look for
- Success criteria

**Test approach:**
[How to verify this experiment - commands to run, manual checks, or both]
- Agent can test: [Commands the agent can run automatically]
- User must verify: [Manual verification steps if needed]

**Result:** ⏸️ Awaiting testing

[Results sections will be added after testing]
```

**Placement:**
- If document is new (just created): Add as first section
- If document exists: Add at end of document (before any appendices/references if present)
- Leave blank line before and after section

**Creation method:**
- Use Write tool if creating new document
- Use Edit tool if adding to existing document
- Confirm to user: "Created Experiment History section in [plan_path]"
</CreateExperimentHistorySection>

---

<ProposeCodeWithContext>
**Goal:** Agent proposes code changes with full context

**Requirements:**
Agent must provide:
1. **File location and line numbers** - Exact location of changes
2. **Surrounding context** - Enough code context to understand what's being changed
3. **Explanation** - Why this change should work
4. **Code snippet** - The actual code to be added/modified/removed

**Format to user:**
```
## Proposed Code Change

**File:** [file_path]:[line_numbers]

**Current code:**
[surrounding context before]
[code to be changed]
[surrounding context after]

**Proposed change:**
[new code with inline comments explaining changes]

**Why this should work:**
[explanation of hypothesis and reasoning]
```

**After presenting proposal, proceed to <UserApprovalProposal/>**
</ProposeCodeWithContext>

---

<UserApprovalProposal>
**Goal:** Get user approval before proceeding

Present to user:

## Experiment Proposal

[Summary of proposed change]

## Available Actions
- **approve** - Proceed with experiment (document, implement, install)
- **revise** - Modify the proposal
- **cancel** - Abort this experiment

Wait for user response.

**If approve**: Proceed to <DocumentExperimentToPlan/>
**If revise**: Return to <ProposeCodeWithContext/> with user's feedback
**If cancel**: Stop execution
</UserApprovalProposal>

---

<DocumentExperimentToPlan>
**Goal:** Add experiment entry to plan document before implementing

**DECISION:** Auto-increment experiment numbers by counting existing attempts

**DECISION:** Ask user for test approach if not clear from context

**Steps:**
1. Scan "Experiment History" section for all entries matching "### Attempt N:"
2. Find highest N and use N+1 as the next attempt number
3. **Determine test approach:**
   - Analyze context to infer how this change should be tested
   - If clear from context (e.g., "test with Camera type", "run brp_all_type_guides"):
     - Use inferred test approach
   - If not clear from context:
     - Ask user: "How will you test this change?"
     - Wait for user response
   - Document the test approach in experiment entry
4. Generate experiment entry using <ExperimentTemplate/> with auto-incremented number
5. Insert entry at the END of the "Experiment History" section
6. Use Edit tool to add the entry to the plan document
7. Confirm to user: "Documented as Attempt N in [plan_path]"

**DO NOT:**
- Modify existing experiment entries
- Add entry before implementation (this creates permanent record)
- Skip this step (documentation before action is critical)
</DocumentExperimentToPlan>

---

<ImplementChange>
**Goal:** Make the proposed code changes

**Steps:**
1. Use Edit tool to implement the proposed changes
2. Run `cargo build` to verify compilation
3. Run `cargo +nightly fmt` to format code
4. Show user compilation results
5. If compilation fails: Ask user whether to fix or abort
6. If compilation succeeds: Proceed to <InstallAndWaitForReload/>

**Important:**
- Make ONLY the changes proposed in <ProposeCodeWithContext/>
- Do not make additional "improvements" or changes
- If you discover issues during implementation, stop and inform user
</ImplementChange>

---

<CompleteImplementationAndStop>
**Goal:** Finish implementation and create natural stopping point for user testing

**DECISION:** One-at-a-time experiments with natural stopping points (not just MCP reloads)

**Context-Aware Stopping:**
The stopping point depends on what type of experiment was performed:

**For MCP tool changes:**
1. Run `cargo install --path mcp`
2. Present MCP reload instructions:
```
## MCP Server Reload Required

**Installation complete.** Changes have been installed but will not take effect until you reload the MCP server.

**Next steps:**
1. Exit this conversation
2. Reload the MCP server (your editor will automatically restart the subprocess)
3. Return and run: `/experiment [plan_path] document`

**What to test:** [Brief description]
**Test command:** [Exact command if applicable]
```
3. Stop execution - user will return after reload

**For regular code changes (non-MCP):**
1. Run appropriate build command (e.g., `cargo build`, `cargo test`)
2. Present testing instructions:
```
## Implementation Complete - Ready for Testing

**Changes have been built successfully.**

**Next steps:**
1. Test the changes: [specific test instructions]
2. When ready to document results, run: `/experiment [plan_path] document`

**What to test:** [Brief description]
**Test command:** [Exact command if applicable]
```
3. Stop execution - user will test and return

**For non-code experiments:**
1. Complete the implementation steps
2. Present verification instructions:
```
## Experiment Complete - Ready for Verification

**Changes have been implemented.**

**Next steps:**
1. Verify the results: [specific verification steps]
2. When ready to document results, run: `/experiment [plan_path] document`

**What to verify:** [Brief description]
```
3. Stop execution - user will verify and return

**Detection Logic:**
- Check if changes are in `mcp/` directory → MCP reload
- Check if changes are Rust code → Build and test
- Otherwise → General verification

**Agent vs User Testing:**
Based on the documented test approach, determine what the agent can do:
- **Agent can test:** Running commands, checking output, comparing results
  - Example: `mcp__brp__brp_type_guide`, `cargo test`, `cargo build`
  - Agent runs these automatically in "document" mode
- **User must test:** Manual verification, visual inspection, MCP reload required
  - Example: "Verify in editor", "Check that root_example appears", "Reload MCP and test"
  - Agent asks user for results in "document" mode
- **Combination:** Agent runs commands, user verifies output
  - Agent runs what it can, then asks user to verify results
</CompleteImplementationAndStop>

---

<TestAndDocumentResults>
**Goal:** Run tests and gather results (agent-executed or user-provided)

**DECISION:** Context determines whether agent runs tests or asks user for results

**Steps:**
1. **Read the experiment entry** to understand documented test approach
2. **Determine testing capability:**
   - Parse test approach from experiment entry
   - Identify what agent can do vs what requires user
3. **Execute agent-capable tests:**
   - If test approach includes commands agent can run:
     - Execute those commands
     - Capture output
     - Present results to user
   - Examples: `cargo test`, `cargo build`, MCP tool calls (after reload)
4. **Gather user test results:**
   - If test approach requires user actions:
     - Ask user for test results:
```
## Experiment Testing

Please provide test results for the most recent experiment:

**Available responses:**
- **success** - Everything worked as expected
- **failed** - Did not work as expected
- **partial** - Some aspects worked, others didn't

After selecting status, please provide:
- What you observed
- Any relevant output, logs, or error messages
- Comparison with expected outcome
```
   - Wait for user response
5. **Analyze all results:**
   - Combine agent-executed results with user-provided results
   - Compare against expected outcome in experiment entry
   - Identify what worked and what didn't
   - If failed/partial: Identify root cause
6. Proceed to <UpdateExperimentEntry/>
</TestAndDocumentResults>

---

<UpdateExperimentEntry>
**Goal:** Update the experiment entry with results

**Steps:**
1. Find the most recent experiment entry in the plan (last entry in Experiment History)
2. Update the **Result:** field from "⏸️ Awaiting approval" or "⏸️ Awaiting testing" to:
   - `✅ **SUCCESS**` if fully successful
   - `❌ **FAILED**` if failed
   - `⚠️ **PARTIAL SUCCESS**` if partial
3. Add results sections based on status:

**For SUCCESS:**
```
**What worked:**
- [Bullet points of what succeeded]

**Verification:**
- [Evidence from testing]

**Conclusion:** [Brief summary]
```

**For FAILED:**
```
**What happened:**
- [What actually occurred]

**Root cause:**
- [Why it failed]

**Next step:** [Recommended next experiment or approach]
```

**For PARTIAL:**
```
**What worked:**
- [Successful aspects]

**What failed:**
- [Failed aspects]

**Root cause of failures:**
- [Analysis]

**Next step:** [What to try next]
```

4. Use Edit tool to update the plan document
5. Show user the updated experiment entry
</UpdateExperimentEntry>

---

<ExperimentTemplate>
**Template for new experiment entries:**

```markdown
### Attempt [N]: [Brief Description] ([DATE])

**Hypothesis:** [One sentence: What we think will work and why]

**Analysis:**
[Understanding of the current problem - can be multiple paragraphs]
- Why previous attempts didn't work (if applicable)
- Key insight that motivates this approach
- What makes this attempt different

**Change Location:** [file_path]:[line_numbers]

**What we're changing:**
[Explanation of the changes in plain English]

**Code:**
\`\`\`rust
[Code snippets showing the changes]
\`\`\`

**Expected outcome:**
[Specific, measurable expectations]
- What should change in behavior
- What values/output to look for
- Success criteria

**Test approach:**
[How to verify this experiment - commands to run, manual checks, or both]
- Agent can test: [Commands the agent can run automatically]
- User must verify: [Manual verification steps if needed]

**Result:** ⏸️ Awaiting testing

[Results sections will be added after testing]
```

**Field guidelines:**
- **Hypothesis:** One clear sentence, testable
- **Analysis:** Can reference previous attempts by number
- **Change Location:** Specific enough to find the code quickly
- **Code:** Show actual code, not pseudocode
- **Expected outcome:** Specific enough to verify success/failure
- **Test approach:** Split into agent-executable commands vs user verification steps
- **Result:** Start with awaiting status, update after testing
</ExperimentTemplate>

---

<ExperimentWorkflowReminder>
**General Experiment Workflow:**

The experiment command supports any type of experimental change with a two-phase workflow:

1. **Propose** mode (implementation phase):
   - Propose changes
   - Document to plan
   - Implement code
   - Build/install as needed
   - Stop at natural checkpoint

2. **User tests/verifies** (manual step)
   - For MCP changes: Reload server
   - For code changes: Run tests
   - For other experiments: Verify results

3. **Document** mode (after verification):
   - Report test results
   - Document outcomes
   - Analyze what worked/failed

This two-phase approach ensures each experiment is tested before proceeding to the next, maintaining experimental rigor.
</ExperimentWorkflowReminder>

---

## Examples

**Start new experiment:**
```
/experiment .claude/plans/root-example-assembly-approach.md
```
Agent proposes code, documents to plan, implements, installs, and waits for reload.

**Document results after testing:**
```
/experiment .claude/plans/root-example-assembly-approach.md document
```
Agent asks for test results, analyzes, and updates the plan.

**Auto-detect plan:**
```
/experiment
```
Finds most recent plan in `.claude/plans/` and starts proposal mode.
