Create a todo list for this plan using your todo tool, then STOP.

**CRITICAL DIRECTIVE**: If during implementation you need to structurally deviate from what the user asked for in the planning phase, you MUST immediately STOP and clarify with the user how to proceed. Do not continue with structural changes without explicit approval.

**CRITICAL DIRECTIVE**: If you reach a context threshold, and are not complete, you must not stop. You must continue. If you are about to auto-compact, you must write out a file indicating what you need to know to continue correctly after the auto-compact. Unless you reach the condition above about structurally deviating from the plan and require guidance, under no other circumstance should you stop until finish the plan.

<Setup>
- Set terminal title: `echo -e "\e]2;Implementation: [brief description]\007"`
- Order todos by dependency to minimize compiler errors
- Use same priority for all items to preserve order
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
- [ ] Run `cargo build && cargo +nightly fmt` and fix errors following <WarningRules>

Build Heuristic:
- Build after changes that should compile independently (new functions, modules, types)
- For breaking changes (API changes, renames, signature changes):
  - If few callers (1-3): Fix them together, then build
  - If many callers (4+): Fix ONE caller first, build to verify approach, then fix rest
- Always `cargo build && cargo +nightly fmt` before moving to unrelated changes to catch errors early
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
- [ ] Subagent: STOP and return control
- [ ] Main: Task new subagent for code review (read-only)
  - Focus: duplication, complexity, plan adherence
- [ ] Review suggestions with user
</ReviewTodos>

IMPORTANT: After creating the todo list, STOP. Do not begin implementation.
