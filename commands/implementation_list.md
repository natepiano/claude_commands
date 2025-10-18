<Persona>
@~/.claude/shared/personas/principal_engineer_persona.md

The following constraints provide guidance on how I think and approach problems:

@~/.claude/shared/subagent_instructions/code_review_instructions.md
</Persona>

## Arguments
If `$ARGUMENTS` is provided, it should be the name of a `plan*.md` file in the project root that will be the basis for implementation. For example:
- `plan_enum_generation.md`
- `plan_mutation_system.md`
- `plan_api_redesign.md`

If no arguments are provided, the command will work with the plan document currently being worked on in the session.

## Overview
Create a todo list for the specified plan using your todo tool, then STOP.

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 0:** Execute <Persona/> to adopt the Principal Engineer persona
    **STEP 1:** Execute <PlanDocumentSelection/>
    **STEP 2:** Execute <ReviewFindingsProcessing/>
    **STEP 3:** Execute <Setup/>
    **STEP 4:** Execute <ImplementationTodos/>
    **STEP 5:** Execute <ValidationTodos/>
    **STEP 6:** Execute <ReviewTodos/>
</ExecutionSteps>

<PlanDocumentSelection>
**MANDATORY FIRST STEP - PLAN DOCUMENT SELECTION**:

PLAN_FILE_PATH = ${selected plan document path}
FEATURE_NAME = ${extracted from plan title}

1. If `$ARGUMENTS` is provided:
   - Set PLAN_FILE_PATH = $ARGUMENTS
   - Read the specified plan document from the project root
   - If file not found, display error: "Plan file ${PLAN_FILE_PATH} not found in project root. Please verify the filename and try again."
   - If file exists but is not readable, display error: "Plan file ${PLAN_FILE_PATH} exists but cannot be read. Please check file permissions."
2. If no arguments: Use the plan document currently being worked on in the session
3. Verify the plan document exists and is readable
4. Check if the plan document is checked into git using: `git status ${PLAN_FILE_PATH}`
5. If it is not checked in, commit ONLY the plan file:
   - Run: `git add ${PLAN_FILE_PATH}` (ONLY the plan file, nothing else)
   - Run: `git commit -m "docs: add implementation plan for ${FEATURE_NAME}"`
   - DO NOT use `git add .` or `git add -A`
6. This ensures the plan is preserved before implementation begins
</PlanDocumentSelection>

<ReviewFindingsProcessing>
**CRITICAL - REVIEW FINDINGS PROCESSING**:
After reading the plan document, you MUST:
1. Search for ALL review sections (e.g., "Design Review Skip Notes", review findings)
2. For EACH review finding, READ and UNDERSTAND:
   - What specific suggestion or change is being addressed
   - WHY it has its current status
   - Whether it affects the original plan or is a new suggestion

3. Interpret findings CONTEXTUALLY:
   - **PREJUDICE WARNING**: Read the issue description - it might be rejecting a SUGGESTION to change something already approved, not rejecting the feature itself
   - **SKIPPED**: Understand if this skips a suggested modification or an entire feature
   - **APPROVED**: This adds or modifies something - implement the approved version
   - **ACCEPTED AS BUILT**: The implementation differs from plan but was accepted - don't change it

4. Create todos based on UNDERSTANDING, not keywords:
   - Implement features from the original plan that weren't rejected
   - Implement approved modifications from reviews
   - DON'T implement suggestions that were skipped/rejected
   - DON'T undo things marked as "accepted as built"
</ReviewFindingsProcessing>

It is **CRITICAL** that you think deeply about the implementation according to the instructions given here so that you make the best possible implementation todo list.

**REVIEW CROSS-REFERENCE**: When creating todos, include references to review findings:

DESCRIPTION = ${specific feature description}

- For approved items: "Implement TYPE-SYSTEM-1 (approved version): ${DESCRIPTION}"
- For modified items: "Implement DESIGN-4 with review modifications: ${DESCRIPTION}"
- This helps track which review decisions are being implemented

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
BRIEF_DESCRIPTION = ${summary of implementation scope}

- Order todos by dependency to minimize compiler errors
</Setup>

<WarningRules>
NEVER fix warnings by:
- Adding #[allow(dead_code)] - remove the dead code instead
- Prefixing with underscore - remove unused arguments/variables
Exception: Code you just added that you'll use immediately
</WarningRules>

<CodingGuidelines>
**CRITICAL CODING STYLE REQUIREMENTS**:

1. **Function Length Limits**:
   - Functions MUST NOT exceed 100 lines
   - If you find yourself writing a function longer than 100 lines, STOP and refactor:
     - Extract logical sections into well-named helper functions
     - Each helper should have a single, clear responsibility
     - The main function should read like a high-level overview
   - This is a hard requirement - violating it is a coding style error

2. **Code Organization**:
   - Keep functions focused on a single purpose
   - Use descriptive names for helper functions that explain what they do
   - Group related helper functions together
</CodingGuidelines>

<ReviewFindingsInterpretation>
**How to interpret review findings for todo generation - READ CAREFULLY:**

**Key Interpretation Patterns**:

1. **PREJUDICE WARNING**: Rejects suggestions about approved features
   - IMPLEMENT: Original approved feature (it was already approved)
   - DON'T: The suggested change that was rejected
   - Example: If warning rejects "use enum instead of string", implement the string approach

2. **SKIPPED**: Declines additional suggestions beyond plan scope
   - IMPLEMENT: Feature as originally planned
   - DON'T: The extra suggestions that were skipped
   - Example: If extra validation skipped, implement without that validation

3. **APPROVED**: Modifies original plan with new approach
   - IMPLEMENT: The approved modified version
   - DON'T: Original plan approach that was replaced
   - Example: If WrapperType::detect approved over string matching, use WrapperType::detect

4. **ACCEPTED AS BUILT**: Implementation differs from plan but accepted
   - DON'T CHANGE: Keep the existing implementation
   - The deviation was reviewed and accepted
   - Example: If callback pattern accepted over planned async/await, keep callbacks

5. **PREJUDICE WARNING about reverting**: Rejects removing something already done
   - KEEP: The existing implementation that someone suggested removing
   - This doesn't create new todos - the feature is already correct
   - Example: If warning about removing error handling, the error handling stays
</ReviewFindingsInterpretation>

<ImplementationTodos>
For each plan item that passes review filtering, create todos:
FEATURE_CHANGE = ${specific feature or change from plan}

- [ ] Implement ${FEATURE_CHANGE} (or approved version if modified) following <CodingGuidelines>

Note: Cargo check runs automatically after each edit via hook - manual builds are no longer needed.

Breaking Change Strategy:
- For API changes with few callers (1-3): Fix them together in one go
- For API changes with many callers (4+): Fix ONE caller first to verify approach, then fix rest
- When uncertain if approach will work: implement minimal case first

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
