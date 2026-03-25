<Persona>
@~/.claude/shared/personas/principal_engineer_persona.md

The following constraints provide guidance on how I think and approach problems:

@~/.claude/shared/constraints/code_review_constraints.md
</Persona>

<RunClippy>
CLIPPY_FLAGS = --workspace --all-targets --all-features -- -D warnings

Execute: `cargo clippy ${CLIPPY_FLAGS} ${ARGUMENTS:-}`

If $ARGUMENTS provided, use as additional flags.
If different base configuration needed, user can override CLIPPY_FLAGS.

Error Handling:
- **Environmental Issues (Stop execution):** If clippy fails due to missing Cargo.toml, network issues, or missing toolchain, inform user: "Clippy cannot run - environment setup required. Check for Cargo.toml and valid Rust workspace." Then exit.
- **Compilation Errors (Process as todos):** If clippy fails due to compilation errors, treat these as high-priority todos alongside any warnings found.

Capture all output for analysis - both successful warnings and compilation errors become todos.
</RunClippy>

<RunMend>
Execute: `cargo mend`

Error Handling:
- **Environmental Issues (Stop execution):** If mend fails due to missing Cargo.toml or missing toolchain, inform user: "cargo mend cannot run - environment setup required." Then exit.
- **Warnings/Issues (Process as todos):** All visibility issues reported by mend become todos alongside clippy issues.

Capture all output for analysis.
</RunMend>

<CreateBatchTodoList>
Create a comprehensive todo list combining all clippy AND mend issues:
- Group related issues in same function/struct into single todos when logical
- Each todo includes fix description and affected file locations
- Label each todo with its source (clippy or mend) for clarity
- Present complete batch for user decision
- Note: Hook automatically provides cargo check feedback on edit - no explicit build commands needed
</CreateBatchTodoList>

<BatchDecisionPoint>
Present the complete batch of clippy fixes:

## Issues Found
**Clippy**: [clippy_count] issues across [clippy_file_count] files
**Mend**: [mend_count] issues across [mend_file_count] files
[List all todos with descriptions, grouped by source]

## Available Actions
- **proceed** - Fix all issues using standard clippy guidance
- **change** - Modify the approach (specify changes)
- **stop** - Cancel clippy fixes without making changes

Please select one of the keywords above.
</BatchDecisionPoint>

<BatchExecution>
**proceed**: Apply all fixes systematically following <FixingGuidelines/>.

**Hook Integration**: Expect automatic cargo check context injection after each edit:
- Hook will automatically run cargo check and inject error/warning context
- Take immediate action on injected error/warning information to resolve issues
- If cargo check context is insufficient for diagnosis, run `cargo build` to get full error details
- Continue fixing systematically, responding to each hook feedback cycle

**change**: Ask user: "What modifications would you like to the fixing approach?" Then apply their specified changes to the batch.

**stop**: Exit without applying any fixes.

After batch completion: Display summary of fixes applied and any remaining issues.
</BatchExecution>

<FixingGuidelines>
**Important rules:**
- Do not fix warnings by marking code as dead - remove dead code
- Do not fix warnings by prefixing arguments/variables with _ - remove if unused
</FixingGuidelines>

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 0:** Execute <Persona/> to adopt the Principal Engineer persona
**STEP 1:** Execute <RunClippy/> AND <RunMend/> (run both to collect all issues) - Report: "Found [clippy_count] clippy issues and [mend_count] mend issues"
**STEP 2:** Execute <CreateBatchTodoList/> - Report: "Created batch of [todo_count] grouped fixes"
**STEP 3:** Execute <BatchDecisionPoint/>
**STEP 4:** Execute <BatchExecution/> with progress: "Processing fix [current] of [total]: [description]"
**STEP 5:** Completion summary: "✓ Fixes complete. Applied: [applied_count], Hook feedback cycles: [cycle_count], Issues resolved: [resolved_count]"
</ExecutionSteps>
