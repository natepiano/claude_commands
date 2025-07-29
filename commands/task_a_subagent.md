<PreChecks>
1. Display current working directory: `pwd`
2. Confirm working directory with user
3. Check for TECH_DEBT.md - read if exists
</PreChecks>

<Arguments>
Optional: Plan document filename (e.g., plan-feature.md)
- If provided, instruct the subagent to read and follow this plan
- If not provided, subagent will work from the plan/context you provide directly
</Arguments>

<Steps>
1. Write todo list to `.todo.json`
2. Commit `.todo.json` if there are any other files uncommitted DO NOT COMMIT THEM
3. Task subagent with the content between <SubagentInstructions> and </SubagentInstructions> tags AND the current plan/context needed to work effectively, including current working directory
   - If $ARGUMENTS provided: Include instruction to read the plan document first
4. Read updated `.todo.json` after completion
5. Sync to TodoWrite tool
6. Display final status
7. Delete `.todo.json` ONLY if all tasks completed
</Steps>

<SubagentInstructions>
**STEP 1: VERIFY WORKING DIRECTORY**
- Current working directory: [REPLACE WITH ACTUAL pwd OUTPUT]
- FIRST ACTION: Run `pwd` to confirm you're in the correct directory
- If wrong directory, STOP and ask user to clarify

**STEP 2: READ .todo.json**
- MANDATORY: Read .todo.json file to understand all tasks
- This file contains your work queue - you MUST follow it

[IF $ARGUMENTS PROVIDED, ADD THIS SECTION:]
**STEP 3: READ PLAN DOCUMENT**
- Read [PLAN_DOCUMENT_NAME] for implementation plan
- Follow the plan's specifications and requirements

**STEP 4: BEGIN TODO WORKFLOW - NEVER SKIP THIS**

**MANDATORY 3-STEP PROCESS FOR EVERY TASK:**

**STEP A**: Update .todo.json - change task status from "pending" to "in_progress"
**STEP B**: Do the actual work for that task  
**STEP C**: Update .todo.json - change task status to "completed"

**YOU CANNOT PROCEED TO STEP B WITHOUT COMPLETING STEP A**
**YOU CANNOT MOVE TO NEXT TASK WITHOUT COMPLETING STEP C**

**If you do ANY work without first updating .todo.json to "in_progress", you are violating instructions.**

.todo.json structure:
[
  {
    "id": "string",
    "content": "string", 
    "status": "pending|in_progress|completed",
    "priority": "high|medium|low",
    "notes": "string"
  }
]

**REMINDER: If you work on ANY task without updating .todo.json status, you are failing to follow instructions.**

TECH_DEBT.md usage:
- Update when creating technical debt
- Document context before hitting limits
- Track identified duplication/complexity
- **MANDATORY**: If you remove/disable code or write TODO due to complexity, document it in TECH_DEBT.md with the decision and reasoning

**MANDATORY TODO UPDATE CHECKPOINTS:**
- Before ANY work: Update .todo.json status to "in_progress"
- After completing ANY task: Update .todo.json status to "completed"
- When documenting progress: Update "notes" field in .todo.json
- When encountering blockers: Update "notes" field with details

**BEFORE YOU DO ANYTHING ELSE: Update the .todo.json file status for the task you're about to work on**

When approaching context window limit:
1. **UPDATE .todo.json**: Add detailed notes about progress to current task
2. Save all work but DO NOT commit (except .todo.json updates)
3. Tell main agent: "Context limit reached. Please task new subagent to continue from .todo.json"
4. DO NOT delete .todo.json - next subagent needs it

Continue working unless:
- Context window nearly full → **UPDATE .todo.json FIRST**, then request handoff to new subagent  
- Missing required information → **UPDATE .todo.json notes**, then request from main agent
- User intervention needed → **UPDATE .todo.json notes**, then explain issue and stop

**FINAL REMINDER: Every single task transition must involve updating .todo.json. If you complete any work without updating the file, you have failed.**

</SubagentInstructions>

<HandoffInstructions>
When tasking continuation subagent:
- Use subagent with the content between <SubagentInstructions> and </SubagentInstructions> tags above AND the current plan/context needed to work effectively, including current working directory
- Add: "Continue from existing .todo.json - do not create new one"
- Emphasize: Read .todo.json first to understand progress
- Include any context from previous subagent's final message
</HandoffInstructions>
