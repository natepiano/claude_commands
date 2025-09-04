## Arguments
If `$ARGUMENTS` is provided, it should be the name of a `plan*.md` file in the project root that will be the basis for implementation. For example:
- `plan_enum_generation.md`
- `plan_mutation_system.md`
- `plan_api_redesign.md`

If no arguments are provided, the command will work with the plan document currently being worked on in the session.

## Overview
Create a todo list for the specified plan using your todo tool, then STOP.

<PlanDocumentEvaluation>
**MANDATORY FIRST STEP - PLAN DOCUMENT SELECTION**:
1. If `$ARGUMENTS` is provided: Read the specified plan document from the project root
2. If no arguments: Use the plan document currently being worked on in the session
3. Verify the plan document exists and is readable
4. Check if the plan document is checked into git
5. If it is not checked in, create an appropriate commit message for it and check it in
6. This ensures the plan is preserved before implementation begins
</PlanDocumentEvaluation>

It is **CRITICAL** that you think deeply about the implementation according to the instructions given here so that you make the best possible implementation todo list.

**CRITICAL DIRECTIVE**: If during implementation you need to structurally deviate from what the user asked for in the planning phase, you MUST immediately STOP and clarify with the user how to proceed. Do not continue with structural changes without explicit approval.

**CRITICAL DIRECTIVE**: If you reach a context threshold, and are not complete, you must not stop. You must continue. When approaching auto-compact:
1. Write a `.handoff_notes.md` file with:
   - Current todo item being worked on
   - Any partial work or discoveries made
   - Specific implementation decisions that deviate from the plan
   - Any gotchas or issues encountered
2. Continue working - let the auto-compact happen
3. After auto-compact, read `.handoff_notes.md` first to resume seamlessly

Unless you reach the condition about structurally deviating from the plan and require guidance, under no other circumstance should you stop until you finish the plan.

<Setup>
- Set terminal title: `echo -e "\e]2;Implementation: [brief description]\007"`
- Order todos by dependency to minimize compiler errors
</Setup>

<WarningRules>
NEVER fix warnings by:
- Adding #[allow(dead_code)] - remove the dead code instead
- Prefixing with underscore - remove unused arguments/variables
Exception: Code you just added that you'll use immediately
</WarningRules>


<ImplementationTodos>
For each plan item, create paired todos:
- [ ] Implement the specific feature/change from plan
- [ ] Run `~/.claude/commands/bash/build-check.sh` and fix errors following <WarningRules>

Build Heuristic:
- Build after changes that should compile independently (new functions, modules, types)
- For breaking changes (API changes, renames, signature changes):
  - If few callers (1-3): Fix them together, then build
  - If many callers (4+): Fix ONE caller first, build to verify approach, then fix rest
- Always `~/.claude/commands/bash/build-check.sh` before moving to unrelated changes to catch errors early
- When uncertain if approach will work: implement minimal case first and build

After every 3-5 implementation items, add:
- [ ] Run `cargo clippy --workspace --all-targets --all-features -- -D warnings`
- [ ] Fix all issues following <WarningRules>
</ImplementationTodos>

<ValidationTodos>
After all implementation todos, add:
- [ ] Run `cargo nextest run`
- [ ] STOP if test failures - review with user
</ValidationTodos>

<ReviewTodos>
Final todos:
- [ ] Clean up `.handoff_notes.md` if it exists (delete the file)
- [ ] Subagent: STOP and return control
</ReviewTodos>


IMPORTANT: After creating the todo list, STOP. Do not begin implementation.
