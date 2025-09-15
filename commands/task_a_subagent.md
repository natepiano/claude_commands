# Task A Subagent

**CRITICAL** This command orchestrates task execution through subagents with automatic continuation and post-implementation review.

## MAIN WORKFLOW

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    
    **PREREQUISITE:** Read <MainAgentResponsibilities/> to understand your role
    **STEP 1:** Execute <TaskSetup/>
    **STEP 2:** Execute <TaskCreation/> OR <TaskContinuation/>
    **STEP 3:** Execute <SubagentExecution/>
    **STEP 4:** Execute <SubagentHandoff/> (if needed)
    **STEP 5:** Execute <PostImplementationReview/>
    **STEP 6:** Execute <TaskCompletion/>
    **THROUGHOUT:** Follow <MainAgentCriticalRules/> at all times
</ExecutionSteps>

<TaskContext>
[WORKING_DIRECTORY]: The directory where task will be executed
[PLAN_DOCUMENT]: Optional plan*.md document with implementation specifications
[TODO_FILE]: .todo.json file for tracking task progress
</TaskContext>

## STEP 1: SETUP

<TaskSetup>
    **Determine Task Environment:**
    
    1. Identify working directory from user request or current context
    2. Check for existing `.todo.json` file:
       - If exists: Prepare for continuation (go to <TaskContinuation/>)
       - If not exists: Prepare for new task (go to <TaskCreation/>)
    3. Identify any plan*.md documents mentioned by user
    4. Set terminal title if applicable: `echo -e "\e]2;Task: [brief description]\007"`
</TaskSetup>

## STEP 2: TASK INITIALIZATION

<TaskCreation>
    **For NEW tasks only:**
    
    1. Create `.todo.json` using format defined in <TodoJsonFormat/>
    2. Order tasks by dependency to minimize build issues
    3. If plan document exists, ensure todos align with plan specifications
    4. Stage and commit `.todo.json` only:
       ```bash
       git add .todo.json
       git commit -m "Initialize task tracking for [task description]"
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
    
    1. Use the Task tool with EXACTLY these parameters:
       - description: "Execute tasks from .todo.json"
       - subagent_type: "general-purpose"
       - prompt: The content between <SubagentPrompt> and </SubagentPrompt> tags below
    
    2. **CRITICAL**: When building the prompt, expand ALL tagged references:
       - Replace <TodoJsonFormat/> with the full content from the <TodoJsonFormat> definition
       - Replace <SubagentCriticalRules/> with the full content from that definition
       - Replace <WarningCategorizationLogic/> with the full content from that definition
       - Include actual values for [WORKING_DIRECTORY] and [PLAN_DOCUMENT]
       - For continuations: Add "Continue from existing .todo.json - do not create new one"
    
    3. Wait for subagent to complete
    
    4. After subagent returns:
       - If subagent reports "Context limit reached": Execute <SubagentHandoff/>
       - If subagent reports blocking issue: Stop and address with user
       - Otherwise: Continue to next step
</SubagentExecution>

<SubagentPrompt>
    **CONTEXT:**
    - Working directory: [WORKING_DIRECTORY]
    - Plan document: [PLAN_DOCUMENT] (read this for implementation details)
    - Task tracking: .todo.json file in working directory
    
    **CRITICAL:** Read and follow <SubagentCriticalRules/> throughout execution
    
    **YOUR WORKFLOW:**
    
    <Step1_ReadContext>
        1. Read .todo.json to understand all tasks (see <TodoJsonFormat/> for structure)
        2. If [PLAN_DOCUMENT] specified, read it for implementation requirements
        3. Identify first incomplete task (status: "pending" or "in_progress")
    </Step1_ReadContext>
    
    <Step2_ImplementationCycle>
        For each task in .todo.json:
        
        1. **CRITICAL**: Update .todo.json - mark current task "in_progress"
        2. **THINK DEEPLY** about implementation:
           - What the task actually requires
           - Best approach for correct implementation
           - Edge cases and architecture fit
        3. Implement the task
        4. Update .todo.json - mark "completed" with notes, mark next task "in_progress"
        5. Continue to next task
        
        **MANDATORY**: Never work on ANY task without first updating .todo.json status
    </Step2_ImplementationCycle>
    
    <Step3_BuildValidation>
        After implementation tasks (before marking final task complete):
        
        1. Run `cargo build 2>&1 | grep -A1 -E "warning:|error:|-->" || true`
        2. Apply <WarningCategorizationLogic/> to each warning
        3. For missing implementation: Add new tasks to .todo.json and continue
        4. For unnecessary code: Delete immediately
        5. Only mark complete when build is clean
    </Step3_BuildValidation>
    
    <Step4_ContextManagement>
        If approaching context limit (THIS IS NORMAL):
        1. Update .todo.json with detailed progress notes
        2. Save all work (don't commit except .todo.json)
        3. Return: "Context limit reached - ready for handoff to continue from .todo.json"
        
        Continue working unless:
        - Context window nearly full → Return for handoff (main agent will auto-continue)
        - Missing critical information → Stop and request from main agent
        - User intervention needed → Stop and explain issue
    </Step4_ContextManagement>
    
    **FINAL REMINDERS:**
    - Every task transition MUST update .todo.json
    - Build validation is MANDATORY before completion
    - Context limits trigger handoff, not failure
    - Follow all rules in <SubagentCriticalRules/>
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
    
    1. Run `cargo build 2>&1 | grep -A1 -E "warning:|error:|-->" || true`
    
    2. If warnings exist:
       a. Read the original [PLAN_DOCUMENT] (if exists) for context
       b. Apply <WarningCategorizationLogic/> to each warning
       c. For missing implementation:
          - Create new tasks in .todo.json
          - Execute <SubagentExecution/> to complete these tasks
       d. For unnecessary code:
          - Delete the unused code immediately
    
    3. Only proceed when build is clean or all warnings are justified
</PostImplementationReview>

## STEP 6: COMPLETION

<TaskCompletion>
    **Final steps after implementation truly complete:**
    
    1. Read final .todo.json
    2. Sync completed tasks to TodoWrite tool
    3. Display summary:
       - Tasks completed: [count]
       - Build status: [clean/warnings remaining]
       - Any deviations from plan
    4. Delete .todo.json
    5. Provide implementation summary for user
</TaskCompletion>

## REFERENCE BLOCKS

<MainAgentResponsibilities>
    **As the main agent, you are responsible for:**
    1. Determining the working directory for the task
    2. Creating new .todo.json OR identifying existing one for continuation
    3. Specifying plan documents to subagents (CRITICAL for review phase)
    4. Handling all user interactions and confirmations
    5. Executing post-implementation review to ensure completeness
    6. Managing the automatic continuation chain without user interruption
    7. Making decisions about unused code (delete vs implement)
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

<TodoJsonFormat>
    **Expected JSON structure (each field on its own line for readability):**
    ```json
    [
      {
        "sequence_number": 1,
        "content": "Task description",
        "status": "pending",
        "notes": ""
      },
      {
        "sequence_number": 2,
        "content": "Another task description",
        "status": "pending",
        "notes": ""
      }
    ]
    ```
    
    **Field specifications:**
    - sequence_number: Integer, sequential (1, 2, 3...)
    - content: Clear task description
    - status: "pending" | "in_progress" | "completed"
    - notes: Implementation notes, warnings found, or resequencing reasons
    
    **Formatting requirements:**
    - Each field must be on its own line
    - Proper indentation (2 spaces) for fields within objects
    - Comma after each object except the last
</TodoJsonFormat>

<SubagentCriticalRules>
    **NEVER violate these rules:**
    1. Always update .todo.json to "in_progress" BEFORE working on any task
    2. Never skip build validation after implementation
    3. Always add detailed notes when hitting context limits
    4. Never commit code (only .todo.json updates allowed)
    5. Never assume dead code is intentional - check or remove it
</SubagentCriticalRules>

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