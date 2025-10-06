# experiment

**Purpose:** Guide structured experimentation for bug fixes and feature implementations with automatic documentation to plan files. Designed for MCP development workflows requiring server reloads.

**Usage:** `/experiment [plan_path]`

**Arguments:**
- `plan_path` (optional): Path to plan document (defaults to `.claude/plans/` search)

**Workflow:**
The command automatically detects what to do based on document state:
- No file exists: Create new experiment document → run first experiment
- File exists without Experiment History: Convert to experiment format → run first experiment
- File exists with Experiment History: Detect state from latest entry → continue workflow

<Persona>
@~/.claude/shared/personas/principal_engineer_persona.md

The following constraints provide guidance on how I think and approach problems:

@~/.claude/shared/constraints/code_review_constraints.md
</Persona>

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <Persona/> to adopt the Principal Engineer persona
**STEP 2:** Execute <InitializeExperimentTodos/>
**STEP 3:** Execute <LoadPlanDocument/>
**STEP 4:** Execute <DetectWorkflowState/>
**STEP 5:** Based on detected state, execute appropriate workflow:
  - If **new_experiment**: Execute <ProposeCodeWithContext/> → <UserApprovalProposal/> → <DocumentExperimentToPlan/> → <ImplementChange/> → <CompleteImplementationAndStop/>
  - If **awaiting_results**: Execute <TestAndDocumentResults/> → <UpdateExperimentEntry/>
  - If **ready_for_next**: Execute <ProposeCodeWithContext/> (start next experiment)
</ExecutionSteps>

---

<InitializeExperimentTodos>
**Goal:** Create todo tracking for experiment workflow visibility

**Steps:**
1. Determine which workflow path will be executed (from <DetectWorkflowState/>)
2. Create TodoWrite with appropriate workflow todos:

**For new_experiment workflow:**
```
TodoWrite with todos:
- "Load plan document" (in_progress)
- "Detect workflow state" (pending)
- "Propose code changes" (pending)
- "Get user approval" (pending)
- "Document experiment to plan" (pending)
- "Implement changes" (pending)
- "Complete implementation" (pending)
```

**For awaiting_results workflow:**
```
TodoWrite with todos:
- "Load plan document" (in_progress)
- "Detect workflow state" (pending)
- "Test and gather results" (pending)
- "Update experiment entry" (pending)
```

**For ready_for_next workflow:**
```
TodoWrite with todos:
- "Load plan document" (in_progress)
- "Detect workflow state" (pending)
- "Propose next experiment" (pending)
```

3. Update todos as in_progress when entering each step
4. Mark completed when step finishes successfully
</InitializeExperimentTodos>

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

<DetectWorkflowState>
**Goal:** Determine next action based on document state

**Steps:**
1. Check if Experiment History section exists:
   - If NO section found: Set state = **new_experiment**
   - If YES: Proceed to step 2
2. Scan Experiment History for latest experiment entry (highest attempt number):
   - If no entries exist yet: Set state = **new_experiment**
   - If latest entry contains "⏸️ Awaiting testing": Set state = **awaiting_results**
   - If latest experiment is complete (✅/❌/⚠️): Set state = **ready_for_next**
3. Store detected state for ExecutionSteps branching
</DetectWorkflowState>

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

**Steps:**
1. Mark "Get user approval" todo as in_progress
2. Present to user:

## Experiment Proposal

[Summary of proposed change]

## Available Actions
- **approve** - Proceed with experiment (document, implement, install)
- **revise** - Modify the proposal
- **cancel** - Abort this experiment

Please select one of the keywords above.

3. **STOP and wait for user response.** Do NOT proceed until user provides their decision.
4. Update todos based on response:
   - **If approve**: Mark "Get user approval" as completed, proceed to <DocumentExperimentToPlan/>
   - **If revise**: Keep "Get user approval" in_progress, return to <ProposeCodeWithContext/> with user's feedback
   - **If cancel**: Mark all remaining todos as cancelled, stop execution
   - **If unrecognized input**: Display message:
     ```
     Unrecognized response '${USER_INPUT}'. Please select from: approve, revise, or cancel.
     ```
     Then re-present the Available Actions menu and wait for valid keyword. Loop until valid response received.
</UserApprovalProposal>

---

<DocumentExperimentToPlan>
**Goal:** Add experiment entry to plan document before implementing

**DECISION:** Auto-increment experiment numbers by counting existing attempts

**DECISION:** Ask user for test approach if not clear from context

**DECISION:** Include execution step protocol at top of plan for context recovery

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
4. **If this is the first experiment in the plan:**
   - Add "## Experiment Protocol" section before "## Experiment History"
   - Include the standard protocol so agents can understand the workflow:
```markdown
## Experiment Protocol

When proposing and implementing fixes:

**Step 0: Propose Code with Context**
- Show the exact code change with file location and line numbers
- Include enough surrounding context to understand what's being changed
- Explain why the change should work

**Step 1: Add Experiment to Plan**
- Document the hypothesis, proposed fix, and expected outcome in the Experiment History section
- Wait for user approval before proceeding

**Step 2: Make the Change**
- Implement the proposed code changes
- Build and format the code

**Step 3: Install and Stop**
- Run `cargo install --path mcp`
- Stop and wait for user to reconnect MCP server

**Step 4: Test and Update Plan**
- User reconnects and runs debug protocol
- Document results (success/failure/partial) in the experiment entry
- Analyze what worked and what didn't
```
5. Execute <ExperimentTemplate/> to generate experiment entry with auto-incremented number
6. Insert entry at the END of the "Experiment History" section
7. Use Edit tool to add the entry to the plan document
8. Confirm to user: "Documented as Attempt N in [plan_path]"

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
2. Run `cargo build && cargo +nightly fmt` to verify compilation and format code
3. Show user compilation results
4. If compilation fails: Ask user whether to fix or abort
5. If compilation succeeds: Proceed to <CompleteImplementationAndStop/>

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
3. Return and run: `/experiment [plan_path]`

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
2. When ready to document results, run: `/experiment [plan_path]`

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
2. When ready to document results, run: `/experiment [plan_path]`

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
  - Agent runs these automatically when state is **awaiting_results**
- **User must test:** Manual verification, visual inspection, MCP reload required
  - Example: "Verify in editor", "Check that root_example appears", "Reload MCP and test"
  - Agent asks user for results when state is **awaiting_results**
- **Combination:** Agent runs commands, user verifies output
  - Agent runs what it can, then asks user to verify results
</CompleteImplementationAndStop>

---

<TestAndDocumentResults>
**Goal:** Run tests and gather results (agent-executed or user-provided)

**DECISION:** Context determines whether agent runs tests or asks user for results

**Steps:**
1. Mark "Test and gather results" todo as in_progress
2. **Read the experiment entry** to understand documented test approach
3. **Determine testing capability:**
   - Parse test approach from experiment entry
   - Identify what agent can do vs what requires user
4. **Execute agent-capable tests:**
   - If test approach includes commands agent can run:
     - Execute those commands
     - Capture output
     - Present results to user
   - Examples: `cargo test`, `cargo build`, MCP tool calls (after reload)
5. **Gather user test results:**
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

Please select one of the keywords above.
```
   - **STOP and wait for user response.** Do NOT proceed until user provides test results.
6. **Analyze all results:**
   - Combine agent-executed results with user-provided results
   - Compare against expected outcome in experiment entry
   - Identify what worked and what didn't
   - If failed/partial: Identify root cause
7. Mark "Test and gather results" as completed
8. Proceed to <UpdateExperimentEntry/>
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

## Examples

**Start new experiment (creates plan and runs first experiment):**
```
/experiment
```
Agent detects no plan exists, creates experiment document, proposes code, implements, and waits for testing.

**Continue experiment (auto-detects state):**
```
/experiment .claude/plans/root-example-assembly-approach.md
```
Agent reads plan, detects state from Experiment History:
- If awaiting_results: Asks for test results and updates plan
- If ready_for_next: Proposes next experiment
- If new (no entries): Proposes first experiment

**Convert existing document to experiment format:**
```
/experiment .claude/plans/my-design.md
```
Agent detects document has no Experiment History, adds it, then runs first experiment.
