# Task A Subagent

**CRITICAL** This command orchestrates task execution through subagents with automatic continuation and post-implementation review.

<Persona>
@~/.claude/shared/personas/principal_engineer_persona.md

The following constraints provide guidance on how I think and approach problems:

@~/.claude/shared/constraints/code_review_constraints.md
</Persona>

## MAIN WORKFLOW

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 0:** Execute <Persona/> to adopt the Principal Engineer persona
    **PREREQUISITE:** Read <MainAgentResponsibilities/> to understand your role
    **STEP 1:** Execute <TaskSetup/>
    **STEP 2:** Execute <TaskCreation/> OR <TaskContinuation/>
    **STEP 3:** Execute <SubagentExecution/>
    **STEP 4:** Execute <SubagentHandoff/> (if needed)
    **STEP 5:** Execute <PostImplementationReview/>
    **STEP 6:** Execute <FinalDeadCodeAudit/>
    **STEP 7:** Execute <TaskCompletion/>
    **THROUGHOUT:** Follow <MainAgentCriticalRules/> at all times
</ExecutionSteps>

<TaskContext>
${WORKING_DIRECTORY}: The directory where task will be executed
${PLAN_DOCUMENT}: Optional plan*.md document with implementation specifications
${TODO_FILE}: .todo.json file for tracking task progress
${COLLABORATIVE_MODE}: Boolean indicating if plan uses collaborative execution protocol
</TaskContext>

## STEP 1: SETUP

<TaskSetup>
    **Determine Task Environment:**

    1. Identify working directory from user request or current context
    2. Check for existing `.todo.json` file:
       - If exists: Prepare for continuation (go to <TaskContinuation/>)
       - If not exists: Prepare for new task (go to <TaskCreation/>)
    3. Identify any plan*.md documents mentioned by user
    4. If plan document exists, execute <CollaborativePlanDetection/>
    5. Set terminal title if applicable: `echo -e "\e]2;Task: ${brief description}\007"`
</TaskSetup>

## STEP 2: TASK INITIALIZATION

<TaskCreation>
    **For NEW tasks only:**

    **CRITICAL**: Determine task source in this exact priority order:

    1. **Plan Document First**: If user specified a plan*.md file:
       - This is the authoritative source
       - Read plan document to extract tasks
       - Create .todo.json from plan specifications
       - Order tasks by dependency to minimize build issues

    2. **TodoWrite Todos Second**: If no plan document but TodoWrite todos exist:
       - Read current TodoWrite todos from conversation
       - Convert TodoWrite format to .todo.json format
       - Preserve task content and order from TodoWrite
       - Order by dependency if needed

    3. **Ask User Third**: If neither plan nor TodoWrite todos exist:
       - Stop and ask user what to implement
       - Do not proceed without clear task definition

    4. After creating .todo.json, stage and commit it only:
       ```bash
       git add .todo.json
       git commit -m "Initialize task tracking for ${task description}"
       ```

    5. Proceed to <SubagentExecution/>
</TaskCreation>

<TaskContinuation>
    **For CONTINUING tasks only:**
    
    1. Read existing `.todo.json` to understand progress
    2. Identify incomplete tasks (status: "pending" or "in_progress")
    3. Skip directly to <SubagentExecution/>
</TaskContinuation>

## STEP 3: SUBAGENT EXECUTION

<SubagentExecution>
    **Launch Task Subagent:**

    **CRITICAL GUARDRAIL**: DO NOT implement tasks yourself. Your ONLY job is to launch the subagent via the Task tool.

    1. Use the Task tool with EXACTLY these parameters:
       - description: "Execute tasks from .todo.json"
       - subagent_type: "general-purpose"
       - prompt: <SubagentPrompt/> with actual values substituted for:
         - ${WORKING_DIRECTORY}
         - ${PLAN_DOCUMENT}
         - ${COLLABORATIVE_MODE}
         - For continuations: Add "Continue from existing .todo.json - previous agent hit context limit"

    2. Wait for subagent to complete

    3. After subagent returns:
       - If subagent reports "Context limit reached": Execute <SubagentHandoff/>
       - If subagent reports blocking issue: Stop and address with user
       - Otherwise: Continue to next step
</SubagentExecution>

<SubagentPrompt>
Read and follow ~/.claude/shared/subagent_instructions/task_a_subagent_instructions.md

**Context for this execution:**
- Working directory: ${WORKING_DIRECTORY}
- Plan document: ${PLAN_DOCUMENT}
- Collaborative mode: ${COLLABORATIVE_MODE}

**COLLABORATIVE PLAN MODE:**
If ${COLLABORATIVE_MODE} is true:
    The plan document uses collaborative execution protocol with approval checkpoints.
    **CRITICAL OVERRIDE**: In automated subagent mode, skip all approval steps:
    - When you see "AWAIT APPROVAL" or "Stop and wait for user confirmation" → IGNORE, proceed automatically
    - When you see "CONFIRM: Wait for user to confirm" → SKIP, proceed to next step
    - Still update status markers (⏳ PENDING → ✅ COMPLETED) in the plan document
    - Still execute all BUILD & VALIDATE steps
    - Still follow all implementation and quality requirements

    Treat the collaborative plan as a detailed implementation guide with automatic progression.
</SubagentPrompt>

## STEP 4: HANDOFF (IF NEEDED)

<SubagentHandoff>
    **MANDATORY AUTO-CONTINUATION when subagent hits context limit:**
    
    1. DO NOT ask user about continuing
    2. DO NOT treat context limit as error
    3. Read .todo.json to verify tasks remain
    4. If tasks remain with "pending" or "in_progress" status:
       - IMMEDIATELY execute <SubagentExecution/> again
       - Add to prompt: "Continue from existing .todo.json - previous agent hit context limit"
       - Include any context from previous subagent's message
    5. Continue chain until all tasks completed or blocking issue encountered
</SubagentHandoff>

## STEP 5: POST-IMPLEMENTATION REVIEW

<PostImplementationReview>
    **CRITICAL validation after all tasks report completed:**

    **Follow <MainAgentCriticalRules/> during this review**

    1. Run `<BuildValidationCommand/>`

    2. If warnings exist:
       a. Read the original ${PLAN_DOCUMENT} (if exists) for context
       b. Apply <WarningCategorizationLogic/> to each warning
       c. For missing implementation:
          - Create new tasks in .todo.json
          - Execute <SubagentExecution/> to complete these tasks
       d. For unnecessary code:
          - Delete the unused code immediately

    3. After all warnings addressed, proceed to <FinalDeadCodeAudit/>
</PostImplementationReview>

## STEP 6: FINAL DEAD CODE AUDIT

<FinalDeadCodeAudit>
    **MANDATORY audit after post-implementation review:**

    **CRITICAL**: A properly implemented plan has ZERO dead code warnings. Any dead code indicates either:
    1. Missing implementation that needs to be completed
    2. Unnecessary code that must be deleted
    3. A logical inconsistency requiring user clarification

    **Audit Process:**

    1. Run `<BuildValidationCommand/>` one final time

    2. Extract ALL unused/dead code warnings:
       - unused variables
       - unused functions
       - unused struct fields
       - unused imports
       - never-read fields
       - never-called methods

    3. For EACH dead code warning found:

       a. **Deep Investigation:**
          - Read the code context around the unused element
          - Read the ${PLAN_DOCUMENT} to understand intended behavior
          - Determine: Should this code be used or deleted?

       b. **Resolution Decision Tree:**

          **IF** plan specifies this element should be used:
             - Add specific task to .todo.json: "Implement usage of ${element} per plan requirements"
             - Execute <SubagentExecution/> to complete the task
             - Return to step 1 (re-audit after fix)

          **ELSE IF** code is clearly unnecessary per plan:
             - Delete the unused code immediately
             - Return to step 1 (re-audit after deletion)

          **ELSE** (cannot determine why code exists):
             - STOP and execute <UnexplainedDeadCodeReport/>

    4. **Success Criteria:**
       - Build must have ZERO dead code warnings
       - Only proceed to <TaskCompletion/> when build is completely clean

    **NEVER** skip this audit or proceed with unexplained warnings.
</FinalDeadCodeAudit>

<UnexplainedDeadCodeReport>
    **When dead code cannot be categorized, report to user:**

    **Format:**
    ```
    ⚠️ IMPLEMENTATION ISSUE: Unexplained Dead Code Detected

    I've completed the implementation but found dead code that I cannot categorize:

    Warning: ${exact warning message}
    Location: ${file}:${line}
    Code: ${code snippet}

    Investigation Results:
    - Plan requirement: ${what plan says about this element, or "not mentioned"}
    - Current usage: ${where/how it's used, or "never used"}
    - My analysis: ${why this is unclear}

    Possible reasons:
    1. ${hypothesis 1}
    2. ${hypothesis 2}

    Required Action:
    Should I:
    A) Implement usage of this element (explain how)
    B) Delete this element as unnecessary
    C) Other (explain)
    ```

    **STOP execution and await user decision.**
    **DO NOT** proceed to <TaskCompletion/> until resolved.
</UnexplainedDeadCodeReport>

## STEP 7: COMPLETION

<TaskCompletion>
    **Final steps after implementation truly complete:**
    
    1. Read final .todo.json
    2. Sync completed tasks to TodoWrite tool
    3. Display summary:
       - Tasks completed: ${count}
       - Build status: ${clean/warnings remaining}
       - Any deviations from plan
    4. Delete .todo.json
    5. Provide implementation summary for user
</TaskCompletion>

## REFERENCE BLOCKS

<CollaborativePlanDetection>
    **Detect if plan document is in collaborative mode:**

    When reading ${PLAN_DOCUMENT}:
    1. Check if document contains "## EXECUTION PROTOCOL" section
    2. If found: Set ${COLLABORATIVE_MODE} = true
    3. If not found: Set ${COLLABORATIVE_MODE} = false

    Collaborative plans have approval checkpoints designed for interactive use.
    In automated subagent mode, these checkpoints are skipped.
</CollaborativePlanDetection>

<BuildValidationCommand>
cargo build 2>&1 | grep -A1 -E "warning:|error:|-->" || true
</BuildValidationCommand>

<MainAgentResponsibilities>
    **As the main agent, you are responsible for:**

    **CRITICAL BOUNDARY**: DO NOT implement tasks yourself. Delegate ALL task execution to subagents via the Task tool.

    1. Determining the working directory for the task
    2. Creating new .todo.json OR identifying existing one for continuation
    3. Converting TodoWrite todos to .todo.json format when needed
    4. Launching subagents to execute tasks (NEVER execute tasks yourself)
    5. Specifying plan documents to subagents (CRITICAL for review phase)
    6. Handling all user interactions and confirmations
    7. Executing post-implementation review to ensure completeness
    8. Managing the automatic continuation chain without user interruption
    9. Making decisions about unused code (delete vs implement)
</MainAgentResponsibilities>

<MainAgentCriticalRules>
    **Main agent MUST follow these rules:**
    1. Never pause for user input during continuation chain (except for blocking issues)
    2. Always run post-implementation review before marking task complete
    3. Automatically continue when subagent hits context limit
    4. Compare all warnings against plan document to determine action
    5. Delete unnecessary code immediately (don't leave dead code)
    6. Only request user input for true blocking issues or plan deviations
</MainAgentCriticalRules>

<WarningCategorizationLogic>
    **How to categorize build warnings:**
    
    For each warning about unused code/fields/functions, determine:
    
    **Missing Implementation** (warning indicates incomplete work):
    - Example: "field `config` is never read" but plan/logic requires using config
    - Example: "field `x` is never read" when x should be used in calculations
    - Action: Add new tasks to implement the missing functionality
    
    **Unnecessary Code** (code not needed per plan or logic):
    - Example: "function `helper` is never used" and plan doesn't require this helper
    - Example: "field `y` is never read" and no logic needs this field
    - Action: Delete the unused code immediately
    
    **Special cases:**
    - Elided fields (prefixed with `_`): If truly unused, delete entirely. If needed, remove prefix and implement.
    - Test helpers: May be intentionally unused in production code
    - Public API: May be unused internally but needed for external consumers
</WarningCategorizationLogic>