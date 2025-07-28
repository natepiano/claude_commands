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
**CRITICAL: VERIFY WORKING DIRECTORY**
- Current working directory: [REPLACE WITH ACTUAL pwd OUTPUT]
- FIRST ACTION: Run `pwd` to confirm you're in the correct directory
- If wrong directory, STOP and ask user to clarify

[IF $ARGUMENTS PROVIDED, ADD THIS SECTION:]
**PLAN DOCUMENT**
- Read [PLAN_DOCUMENT_NAME] for implementation plan
- Follow the plan's specifications and requirements

**CRITICAL: TODO TRACKING (.todo.json)**
- **UPDATE STATUS IMMEDIATELY** as you work: pending → in_progress → completed
- Update the file BEFORE starting each task, DURING work, and AFTER completion
- Use 'notes' field to document progress, blockers, and partial completion
- Structure: [
    {
      "id": "string",
      "content": "string",
      "status": "pending|in_progress|completed",
      "priority": "high|medium|low",
      "notes": "string"
    }
  ]

**WORKFLOW:**
1. Read .todo.json to understand current state
2. Update task status to 'in_progress' BEFORE starting work
3. Work on the task
4. Update status to 'completed' IMMEDIATELY after finishing
5. Move to next task and repeat

TECH_DEBT.md usage:
- Update when creating technical debt
- Document context before hitting limits
- Track identified duplication/complexity
- **MANDATORY**: If you remove/disable code or write TODO due to complexity, document it in TECH_DEBT.md with the decision and reasoning

When approaching context window limit:
1. Update current todo with detailed notes about progress
2. Save all work but DO NOT commit (except .todo.json updates)
3. Tell main agent: "Context limit reached. Please task new subagent to continue from .todo.json"
4. DO NOT delete .todo.json - next subagent needs it

Continue working unless:
- Context window nearly full → Request handoff to new subagent
- Missing required information → Document in notes, request from main agent
- User intervention needed → Explain issue, stop for user input

</SubagentInstructions>

<HandoffInstructions>
When tasking continuation subagent:
- Use subagent with the content between <SubagentInstructions> and </SubagentInstructions> tags above AND the current plan/context needed to work effectively, including current working directory
- Add: "Continue from existing .todo.json - do not create new one"
- Emphasize: Read .todo.json first to understand progress
- Include any context from previous subagent's final message
</HandoffInstructions>
