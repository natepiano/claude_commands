# Task A Subagent Instructions

This file contains detailed instructions for subagents executing tasks from .todo.json files.

## Task Source Priority

**CRITICAL**: Determine task source in this exact order:

1. **Plan Document First**: If ${PLAN_DOCUMENT} is provided in prompt:
   - Read the plan document for implementation requirements
   - Extract tasks from plan sections
   - Create .todo.json from plan specifications
   - Ensure todos align with plan structure

2. **TodoWrite Todos Second**: If no plan document but TodoWrite todos exist:
   - Read current TodoWrite todos from conversation context
   - Convert TodoWrite format to .todo.json format
   - Preserve task content, status, and notes
   - Maintain task order from TodoWrite

3. **Ask User Third**: If neither plan nor TodoWrite todos exist:
   - Stop and request task specification from user
   - Do not proceed without clear task definition

## Your Workflow

<ContextAnalysis>
1. Check prompt for ${PLAN_DOCUMENT} value
2. If plan document specified: Read it for implementation requirements
3. If no plan document: Check for existing .todo.json file
4. If .todo.json exists: Read to understand all tasks (see <TodoJsonFormat/> for structure)
5. If no .todo.json: Check for TodoWrite todos in conversation
6. Identify first incomplete task (status: "pending" or "in_progress")
</ContextAnalysis>

<TaskImplementation>
For each task in .todo.json:

1. **CRITICAL**: Update .todo.json - mark current task "in_progress"
2. If ${COLLABORATIVE_MODE} is true, also update plan document status markers when starting related steps
3. **THINK DEEPLY** about implementation:
   - What the task actually requires
   - Best approach for correct implementation
   - Edge cases and architecture fit
4. Implement the task
5. Update .todo.json - mark "completed" with notes, mark next task "in_progress"
6. If ${COLLABORATIVE_MODE} is true, mark corresponding plan step as ✅ COMPLETED
7. Continue to next task

**MANDATORY**: Never work on ANY task without first updating .todo.json status
</TaskImplementation>

<BuildValidation>
After implementation tasks (before marking final task complete):

1. Run `<BuildValidationCommand/>`
2. Apply <WarningCategorizationLogic/> to each warning
3. For missing implementation: Add new tasks to .todo.json and continue
4. For unnecessary code: Delete immediately
5. Only mark complete when build is clean
</BuildValidation>

<ContextLimitHandling>
If approaching context limit (THIS IS NORMAL):
1. Update .todo.json with detailed progress notes
2. Save all work (don't commit except .todo.json)
3. Return: "Context limit reached - ready for handoff to continue from .todo.json"

Continue working unless:
- Context window nearly full → Return for handoff (main agent will auto-continue)
- Missing critical information → Stop and request from main agent
- User intervention needed → Stop and explain issue
</ContextLimitHandling>

## Reference Definitions

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

<BuildValidationCommand>
cargo build 2>&1 | grep -A1 -E "warning:|error:|-->" || true
</BuildValidationCommand>

## Final Reminders

- Every task transition MUST update .todo.json
- Build validation is MANDATORY before completion
- Context limits trigger handoff, not failure
- Follow all rules in <SubagentCriticalRules/>
- Respect task source priority: Plan → TodoWrite → Ask User
