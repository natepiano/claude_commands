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
4. If the subagent returns before finishing, use <HandoffInstructions/> from below.
5. Read updated `.todo.json` after completion
6. Sync to TodoWrite tool
7. Do the code review if instructed.
8. Display final status
9. Delete `.todo.json` ONLY if all tasks completed
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

**STEP 4: BEGIN TODO WORKFLOW - NEVER SKIP THIS**
Follow instructions in <UpdateToodoJson/>

**If you do ANY work without first updating .todo.json to "in_progress", you are violating instructions.**

**REMINDER: If you work on ANY task without updating .todo.json status, you are failing to follow instructions.**

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
When approaching context window limit:
1. **UPDATE .todo.json**: Add detailed notes about progress to current task
2. Save all work but DO NOT commit (except .todo.json updates)
3. Tell main agent: "Context limit reached. Please task new subagent to continue from .todo.json"
4. DO NOT delete .todo.json - next subagent needs it
</ContextWindow>

Continue working unless:
- Context window nearly full → **UPDATE .todo.json FIRST**, then request handoff to new subagent as described in <ContextWindow/>
- Missing required information → **UPDATE .todo.json notes**, then STOP and request from main agent
- User intervention needed → **UPDATE .todo.json notes**, then STOP and explain issue to the main agent

**FINAL REMINDERS**:
- Every single task transition must involve updating .todo.json. If you complete any work without updating the file, you have failed.
- Watch for technical debt and use <TechDebtDocument/> to track it if you identify any.
- Watch for reaching context window and follow instructions in <ContextWindow/>

</SubagentInstructions>

The following are main agent instructions in the case of a hand off:

<HandoffInstructions>
Optional: When tasking continuation subagent:
- Use subagent with the content between <SubagentInstructions> and </SubagentInstructions> tags AND the current plan/context needed to work effectively, including current working directory
- Add: "Continue from existing .todo.json - do not create new one"
- Emphasize: Read .todo.json first to understand progress
- Include any context from previous subagent's final message
</HandoffInstructions>
