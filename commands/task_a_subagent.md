<PreChecks>
1. Display current working directory: `pwd`
2. Confirm working directory with user
3. Check for TECH_DEBT.md - read if exists
</PreChecks>

<OptionalPlanDocument>
Optional: Plan document filename (e.g., plan-*.md)
- If we are working on a document from a plan*.md instruct the subagent to read and work from the plan document.
- If not provided, subagent will work from the plan/context you provide directly
</OptionalPlanDocument>


<Instructions>
1. Write todo list to `.todo.json` using this format:

.todo.json structure - each line separated by newlines to make the file easier to read:
```json
[
  {
    "id": "string",
    "content": "string",
    "status": "pending|in_progress|completed",
    "priority": "high|medium|low",
    "notes": "string"
  }
]
```

2. Commit `.todo.json` if there are any other files uncommitted DO NOT COMMIT THEM
3. Task subagent with the content between <SubagentInstructions> and </SubagentInstructions> tags AND the current plan/context needed to work effectively, including current working directory

4. **MANDATORY AUTO-CONTINUATION**: When subagent returns:
   a. Read `.todo.json` to check remaining tasks
   b. If ANY tasks have status "pending" or "in_progress", IMMEDIATELY task another subagent:
      - DO NOT stop to ask user about progress
      - DO NOT treat context limits as a blocking issue
      - Use <HandoffInstructions/> to continue seamlessly
   c. ONLY stop the chain when:
      - All tasks show "completed" status, OR
      - Subagent reports actual blocking issue (test failures, missing critical info, explicit errors)
   
5. After ALL tasks completed:
   a. Read final `.todo.json`
   b. Sync to TodoWrite tool
   c. Do code review if instructed
   d. Display final status
   e. Delete `.todo.json`
</Instructions>


<SubagentInstructions>

You'll be working with a .todo.json file. This is the process for updating this file:

<UpdateTodoJson>
1. First task only: update .todo.json - change first task status from "pending" to "in_progress"
2. Do the actual work for that task
3. Update .todo.json - change task status to "completed" and include any notes about this step. Also mark the next task from "pending" to "in_progress". Mark it "completed", add the notes and mark the next one "in_progress" in one edit if you can.
Repeat steps 2 and 3 until done.
</UpdateTodoJson>

Following is the workflow that you must do:

<Workflow>
**STEP 1: VERIFY WORKING DIRECTORY**
- Current working directory: [REPLACE WITH ACTUAL pwd OUTPUT]
- FIRST ACTION: Run `pwd` to confirm you're in the correct directory
- If wrong directory, STOP and ask user to clarify

**STEP 2: READ .todo.json**
- MANDATORY: Read .todo.json file to understand all tasks
- This file contains your work queue - you MUST follow it

[if we're working from a plan*.md then instruct the agent to read the plan document]
**STEP 3: READ PLAN DOCUMENT**
- Read for implementation plan and additional details.
- Follow the plan's specifications and requirements

**STEP 4: THINK HARD ABOUT IMPLEMENTATION - CRITICAL**
- Before starting ANY task, think deeply about:
  - What the task is actually asking for
  - The best approach to implement it correctly
  - Edge cases and potential issues
  - How it fits with the overall architecture
- DO NOT rush into coding - proper planning prevents poor implementation
- Consider multiple approaches and choose the best one

**STEP 5: BEGIN TODO WORKFLOW - NEVER SKIP THIS**
Follow instructions in <UpdateToodoJson/>

**If you do ANY work without first updating .todo.json to "in_progress", you are violating instructions.**

**REMINDER: If you work on ANY task without updating .todo.json status, you are failing to follow instructions.**

**STEP 6: BUILD AND ASSESS WARNINGS - MANDATORY AFTER IMPLEMENTATION**
After completing implementation tasks (but before marking final task complete):
1. Run `cargo build 2>&1 | grep -E "warning:|error:"` to check for warnings
2. For each warning, determine:
   - **Option 1: Code not yet implemented** - Warning indicates missing implementation that's needed
     → **UPDATE .todo.json**: Add new tasks with "pending" status for each missing implementation
     → Mark current task complete with notes about warnings found
     → Continue implementing these new tasks immediately following the standard workflow
   - **Option 2: Dead code that will never be used** - Warning is for code that won't be utilized
     → Remove the dead code immediately before proceeding
     → Do NOT add to .todo.json - just fix it now
3. Examples:
   - "field `bar` is never read" → Check if this field should be used somewhere in the code (Option 1) or if it's truly unnecessary (Option 2)
   - "function `foo` is never used" → Determine if it should be called somewhere (Option 1) or deleted (Option 2)
4. After addressing all warnings, run build again to confirm clean compilation
5. Only mark implementation complete when build has no relevant warnings

The following <TechDebtDocument/>, <ContextWindow/> and <HandoffInstructions/> sections are only to be followed if the situation they describe is occurring.

</Workflow>

<TechDebtDocument>
TECH_DEBT.md usage:
- Update when creating technical debt
- Document context before hitting limits
- Track identified duplication/complexity
- **MANDATORY**: If you remove/disable code or write TODO due to complexity, document it in TECH_DEBT.md with the decision and reasoning
</TechDebtDocument>

<ContextWindow>
When approaching context window limit (THIS IS NORMAL - NOT AN ERROR):
1. **UPDATE .todo.json**: Add detailed notes about progress to current task
2. Save all work but DO NOT commit (except .todo.json updates)
3. Return to main agent with message: "Context limit reached - ready for handoff to continue from .todo.json"
4. DO NOT delete .todo.json - next subagent needs it
5. Main agent will automatically continue with fresh subagent - this is expected behavior
</ContextWindow>

Continue working unless:
- Context window nearly full → **UPDATE .todo.json FIRST**, then return for handoff as described in <ContextWindow/> (main agent will auto-continue)
- Missing required information → **UPDATE .todo.json notes**, then STOP and request from main agent (blocking issue)
- User intervention needed → **UPDATE .todo.json notes**, then STOP and explain issue to main agent (blocking issue)

**FINAL REMINDERS**:
- Every single task transition must involve updating .todo.json. If you complete any work without updating the file, you have failed.
- Watch for technical debt and use <TechDebtDocument/> to track it if you identify any.
- Watch for reaching context window and follow instructions in <ContextWindow/>

</SubagentInstructions>

The following are main agent instructions in the case of a hand off:

<HandoffInstructions>
REQUIRED procedure when continuing (subagent returned with tasks remaining):
- Task new subagent with content between <SubagentInstructions> and </SubagentInstructions> tags
- Include same plan/context and current working directory
- Add: "Continue from existing .todo.json - do not create new one"
- Emphasize: Read .todo.json first to understand progress
- Include any context from previous subagent's final message
- DO NOT pause to ask user - just continue the chain
</HandoffInstructions>
