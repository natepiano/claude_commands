# Upgrade Plan to Collaborative Mode

Convert an existing plan document into a collaborative mode plan with step-by-step execution protocol.

**Arguments**: $ARGUMENTS (path to existing plan document to upgrade)

<Persona>
@~/.claude/shared/personas/architect_persona.md

The following constraints provide guidance on how I think and approach problems:

@~/.claude/shared/constraints/design_review_constraints.md
</Persona>

STATUS_PENDING = ⏳ PENDING
STATUS_COMPLETED = ✅ COMPLETED
STATUS_SUCCESS = ✅
UPGRADE_SUFFIX = -upgraded.md

<SharedWorkflows>
@~/.claude/shared/gap_analysis_workflow.md
</SharedWorkflows>

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 0:** Execute <Persona/> to adopt the Software Architect persona
    **STEP 1:** Execute <AnalyzeAndSequence/>
    **STEP 2:** Execute <ImplementationGapAnalysis/>
    **STEP 3:** Execute <ReviewAndConfirm/>
    **STEP 4:** Execute <GenerateCollaborativePlan/>
    **STEP 5:** Execute <ValidateCompleteness/>
</ExecutionSteps>

## STEP 1: ANALYZE AND SEQUENCE

<AnalyzeAndSequence>
    Use TodoWrite tool to create tracking:
    [
        {content: "Validate input arguments and read plan document", status: "pending", activeForm: "Validating input arguments and reading plan document"},
        {content: "Parse plan content and extract components", status: "pending", activeForm: "Parsing plan content and extracting components"},
        {content: "Analyze dependencies and breaking changes", status: "pending", activeForm: "Analyzing dependencies and breaking changes"},
        {content: "Create optimized build sequence", status: "pending", activeForm: "Creating optimized build sequence"}
    ]

    Mark "Validate input arguments and read plan document" as in_progress.

    If $ARGUMENTS is empty:
        Display error: "Usage: upgrade_plan <path-to-plan-file>"
        Exit

    Use Read tool to read the plan document from $ARGUMENTS.

    If file doesn't exist:
        Display error: "Plan file not found: $ARGUMENTS"
        Exit

    If "## EXECUTION PROTOCOL" already exists:
        Execute <HandleExistingCollaborativePlan/>

    Mark "Validate input arguments and read plan document" as completed.
    Mark "Parse plan content and extract components" as in_progress.

    Parse the plan to extract:
    - All implementation tasks and phases
    - All files to be created/modified
    - Build and test commands
    - Dependencies between changes
    - Current document section order

    Mark "Parse plan content and extract components" as completed.
    Mark "Analyze dependencies and breaking changes" as in_progress.

    Analyze dependencies and create optimal build sequence:
    1. Group related changes that should be tested together
    2. Order by compilation/build dependencies
    3. Ensure each step can be independently validated
    4. Minimize context switching between components

    **BREAKING CHANGE ANALYSIS** (Critical for Rust/compiled languages):
    For each proposed change, analyze:
    - Type signature changes that would break compilation
    - Struct/enum field modifications that affect other files
    - Function signature changes that break callers
    - Trait implementation changes
    - Module visibility or path changes

    Identify change groups that MUST be atomic:
    - If changing a type definition used elsewhere, group with all usage updates
    - If modifying a function signature, group with all call site updates
    - If changing an enum variant, group with all match statement updates
    - Mark these as "ATOMIC GROUP - must be done together to avoid breakage"

    Reorganize sequence to:
    1. **Additive changes first** (new types, functions, fields) - won't break anything
    2. **Atomic breaking change groups** - all related changes together
    3. **Dependent changes** - that rely on previous steps
    4. **Cleanup/removal changes last** - removing deprecated items

    **IMPORTANT**: The PROPOSED_SEQUENCE will become the new document order. Design it so that:
    - Each step corresponds to detailed implementation sections that will be reordered
    - The execution sequence can be followed linearly through the document
    - Related content from multiple original sections can be merged into logical steps
    - **Every step should compile successfully** - no intermediate breakage

    Create PROPOSED_SEQUENCE with:
    - Clear indication of which changes are additive (safe)
    - Which changes form atomic groups (must be done together)
    - Compilation status after each step (✅ builds or ❌ would break)

    Proceed to Step 2.
</AnalyzeAndSequence>

## STEP 2: IMPLEMENTATION GAP ANALYSIS

<ImplementationGapAnalysis>
    Use TodoWrite tool to track: analyzing completeness, comparing code, identifying gaps, user review.

    Display: "🔍 Thinking harder about implementation completeness..."

    Set PLAN_DOCUMENT to $ARGUMENTS for gap analysis workflow.

    Use Task tool:
    - description: "Deep implementation gap analysis"
    - subagent_type: "general-purpose"
    - prompt: <GapAnalysisPrompt/> (from shared workflow)

    Parse response. If no gaps found:
        Display: "✅ Plan Completeness Check: PASSED"
        Proceed to Step 3.

    If gaps found:
        Count critical gaps.
        Display summary:
        ```
        ⏺ ✅ Plan Completeness Check: GAPS FOUND (${gap_count} issues, ${critical_count} CRITICAL)
        ```

        Execute <GapReview/> (from shared workflow) with parsed gaps array.
        After gap review completes, proceed to Step 3.
</ImplementationGapAnalysis>

## STEP 3: REVIEW AND CONFIRM

<ReviewAndConfirm>
    Use TodoWrite tool to create tracking:
    [
        {content: "Present analysis for review", status: "pending", activeForm: "Presenting analysis for review"},
        {content: "Wait for user decision on execution sequence", status: "pending", activeForm: "Waiting for user decision on execution sequence"},
        {content: "Process user feedback", status: "pending", activeForm: "Processing user feedback"}
    ]

    Mark "Present analysis for review" as in_progress.

    Display the proposed collaborative structure:

    ```
    Plan Upgrade Analysis: [filename]
    =====================================

    Current Structure:
    - [X] implementation phases found
    - [Y] files to be modified
    - Migration type: [Atomic/Phased]

    Breaking Change Analysis:
    - [N] additive changes (safe to do independently)
    - [M] atomic change groups (must be done together)
    - All steps verified to compile successfully ✅

    Proposed Collaborative Execution Sequence:

    Step 1: [Name] [SAFE/ATOMIC GROUP]
      Tasks: [brief description]
      Files: [file1, file2]
      Change Type: [Additive/Breaking/Mixed]
      Build Status: ✅ Compiles successfully
      Build: [build command]

    Step 2: [Name] [SAFE/ATOMIC GROUP]
      Tasks: [brief description]
      Files: [file3, file4]
      Change Type: [Additive/Breaking/Mixed]
      Build Status: ✅ Compiles successfully
      Build: [build command]
      Dependencies: Requires Step 1
      Notes: [Any special considerations for atomic groups]

    [... continue for all steps ...]

    Final Step: Complete Validation
      - Run all tests
      - Verify integration
      - Check success criteria
    ```

    Mark "Present analysis for review" as completed.
    Mark "Wait for user decision on execution sequence" as in_progress.

    Ask user: "Does this execution sequence look correct?"

    ## Available Actions
    - **approve** - Generate the collaborative plan
    - **adjust** - Describe what needs to change
    - **abort** - Cancel the upgrade

    Please select one of the keywords above.
    STOP.

    Mark "Wait for user decision on execution sequence" as completed.
    Mark "Process user feedback" as in_progress.

    Handle user response:
    If response is "approve":
        Mark "Process user feedback" as completed.
        Proceed to Step 4

    If response is "adjust":
        Ask: "What needs to be changed in the sequence?"
        Update PROPOSED_SEQUENCE based on feedback
        Mark "Process user feedback" as completed.
        Return to display and ask again

    If response is "abort":
        Mark "Process user feedback" as completed.
        Exit without changes

    Execute <ValidateUserResponse/> with:
        expected_keywords: [approve, adjust, abort]
        option_descriptions: [
            "- **approve** - Generate the collaborative plan",
            "- **adjust** - Describe what needs to change",
            "- **abort** - Cancel the upgrade"
        ]
</ReviewAndConfirm>

## STEP 4: GENERATE COLLABORATIVE PLAN

<GenerateCollaborativePlan>
    Build the complete collaborative plan as an executable document:

    1. Generate EXECUTION_PROTOCOL section with:
       ```markdown
       ## EXECUTION PROTOCOL

       <Instructions>
       For each step in the implementation sequence:

       1. **DESCRIBE**: Present the changes with:
          - Summary of what will change and why
          - Code examples showing before/after
          - List of files to be modified
          - Expected impact on the system

       2. **AWAIT APPROVAL**: Stop and wait for user confirmation ("go ahead" or similar)

       3. **IMPLEMENT**: Make the changes and stop

       4. **BUILD & VALIDATE**: Execute the build process:
          ```bash
          [extracted build commands]
          ```

       5. **CONFIRM**: Wait for user to confirm the build succeeded

       6. **MARK COMPLETE**: Update this document to mark the step as ${STATUS_COMPLETED}

       7. **PROCEED**: Move to next step only after confirmation
       </Instructions>

       <ExecuteImplementation>
           Find the next ${STATUS_PENDING} step in the INTERACTIVE IMPLEMENTATION SEQUENCE below.

           For the current step:
           1. Follow the <Instructions/> above for executing the step
           2. When step is complete, use Edit tool to mark it as ${STATUS_COMPLETED}
           3. Continue to next PENDING step

           If all steps are COMPLETED:
               Display: "${STATUS_SUCCESS} Implementation complete! All steps have been executed."
       </ExecuteImplementation>

       ## INTERACTIVE IMPLEMENTATION SEQUENCE
       ```

       For each step in PROPOSED_SEQUENCE:
         * Create STEP entry with objective, changes, files, build commands
         * Include ${STATUS_PENDING} status marker

       Add final validation step

    2. Restructure the original plan:
       - Place EXECUTION_PROTOCOL at the top (making it executable)
       - **REORDER ALL IMPLEMENTATION SECTIONS**: Rearrange the original detailed implementation sections to match the PROPOSED_SEQUENCE order exactly
         * Extract each original implementation section/task
         * Reorder them to follow the same sequence as the execution steps
         * Merge related sections if multiple original sections map to one execution step
         * Ensure the document flows in the same order as the execution sequence
         * This means readers can follow the document linearly and it matches the build order
       - Update Implementation Strategy to reference protocol steps
       - Add Design Review Skip Notes section if missing
       - **CRITICAL**: The final document structure should be:
         1. Title
         2. EXECUTION PROTOCOL
         3. INTERACTIVE IMPLEMENTATION SEQUENCE
         4. Reordered implementation sections matching execution sequence
         5. Supporting sections (Testing, Risk Assessment, Success Criteria, etc.)

    3. Save the upgraded plan:
       Generate the new filename by replacing .md with ${UPGRADE_SUFFIX}:
       - Example: "plan-something.md" becomes "plan-something${UPGRADE_SUFFIX}"
       - If no .md extension, append "${UPGRADE_SUFFIX}"

       Write the complete restructured plan to the new filename.

       Display: "${STATUS_SUCCESS} Collaborative plan created: [new-filename]"
       Display: "📝 The plan is now executable - run it to start implementation"

    Display initial summary:
    ```
    Upgrade Complete
    ----------------
    ${STATUS_SUCCESS} Created [new-filename] with collaborative execution protocol
    ${STATUS_SUCCESS} Created [N] collaborative execution steps
    ${STATUS_SUCCESS} Organized [X] tasks into buildable chunks
    ${STATUS_SUCCESS} Each step includes validation commands

    Original: $ARGUMENTS (preserved)
    Upgraded: [new-filename] (created)
    ```

    Store new-filename in UPGRADED_FILE variable for Step 5.
    Proceed to Step 5.
</GenerateCollaborativePlan>

## STEP 5: VALIDATE COMPLETENESS

<ValidateCompleteness>
    Use the Task tool with general-purpose subagent to perform comprehensive validation:

    Prompt: "You are tasked with performing a thorough comparison between an original plan document and its upgraded collaborative version. Your goal is to ensure nothing was lost during the transformation.

    Original file: $ARGUMENTS
    Upgraded file: [UPGRADED_FILE]

    Please analyze and report on the following:

    1. **MISSING CONTENT ANALYSIS**
       - List ANY technical details, code snippets, or specifications from the original that are missing in the upgraded version
       - Check for missing comments, notes, warnings, or considerations
       - Verify all file paths, function names, and variable names are preserved
       - Ensure all edge cases and error handling mentions are retained

    2. **IMPLEMENTATION DETAILS CHECK**
       - Compare code examples line by line - are they identical?
       - Check if all TODO items, FIXME notes, or warnings made it through
       - Verify all configuration values, constants, and magic numbers are preserved
       - Ensure all mentioned dependencies and imports are included

    3. **STRUCTURAL INTEGRITY**
       - Confirm all original sections exist (even if reordered)
       - Check that no subsections were accidentally merged or lost
       - Verify all bullet points and numbered lists are complete
       - Ensure all tables, diagrams references, or external links are preserved

    4. **SEMANTIC COMPLETENESS**
       - Check if the upgraded version maintains the same level of detail
       - Verify no explanations or rationales were summarized away
       - Ensure all alternative approaches or rejected solutions are still documented
       - Confirm all success criteria and acceptance tests are preserved

    5. **VERDICT**
       Provide one of these assessments:
       - ✅ COMPLETE: The upgraded file contains 100% of the original content
       - ⚠️ MINOR GAPS: Small formatting or non-critical details missing (list them)
       - ❌ MAJOR GAPS: Important implementation details or sections missing (list them)

    **CRITICAL**: You MUST format your response EXACTLY as specified below. The verdict line must match one of the three exact formats for proper parsing.

    Format your response as:
    ```
    VALIDATION REPORT
    =================

    Missing Content:
    - [List any missing items, or 'None found']

    Implementation Differences:
    - [List any code/detail differences, or 'All details preserved']

    Structural Changes:
    - [List any concerning structural issues, or 'Structure intact']

    Verdict: [COMPLETE/MINOR GAPS/MAJOR GAPS]

    [If gaps found, provide specific file:line references where possible]
    ```

    **PARSING REQUIREMENT**: The verdict line MUST be EXACTLY one of these three options:
    - "Verdict: COMPLETE"
    - "Verdict: MINOR GAPS"
    - "Verdict: MAJOR GAPS"

    Do NOT add emojis, parenthetical notes, or any other text to the verdict line."

    Display: "🔍 Validating content preservation..."

    After receiving the validation report from the subagent:

    If verdict is COMPLETE:
        Display:
        ```
        ${STATUS_SUCCESS} Validation Complete - No Content Lost
        =========================================
        The upgraded plan contains all original content.

        Final Summary:
        - Original: $ARGUMENTS (preserved)
        - Upgraded: [UPGRADED_FILE] (ready to execute)

        To start implementation, run: @[UPGRADED_FILE]
        ```

    If verdict is MINOR GAPS:
        Display:
        ```
        ⚠️ Validation Complete - Minor Gaps Found
        ==========================================

        The following minor items may need attention:
        [List the minor gaps]

        These gaps are non-critical and the plan is usable.

        Final Summary:
        - Original: $ARGUMENTS (preserved)
        - Upgraded: [UPGRADED_FILE] (ready to execute)

        To start implementation, run: @[UPGRADED_FILE]
        ```

    If verdict is MAJOR GAPS:
        Display:
        ```
        ❌ Validation Failed - Major Content Missing
        ============================================

        Critical content was lost during upgrade:
        [List the major gaps with details]

        Action Required:
        The upgraded file has significant gaps and should be reviewed.
        Please manually inspect both files or re-run the upgrade.

        Files:
        - Original: $ARGUMENTS
        - Upgraded: [UPGRADED_FILE] (needs review)
        ```

    Exit after displaying the appropriate summary.
</ValidateCompleteness>

## EXISTING COLLABORATIVE PLAN HANDLING

<HandleExistingCollaborativePlan>
    Use TodoWrite tool to create tracking:
    [
        {content: "Check existing collaborative plan status", status: "pending", activeForm: "Checking existing collaborative plan status"},
        {content: "Present options to user", status: "pending", activeForm: "Presenting options to user"},
        {content: "Process user decision", status: "pending", activeForm: "Processing user decision"}
    ]

    Mark "Check existing collaborative plan status" as in_progress.

    Display: "This plan already contains an EXECUTION PROTOCOL section (collaborative mode)."
    Display: ""
    Display: "You can either:"
    Display: "- Revise the existing collaborative plan according to current upgrade specifications"
    Display: "- Exit and work with the plan as-is"
    Display: ""

    Mark "Check existing collaborative plan status" as completed.
    Mark "Present options to user" as in_progress.

    Ask: "Do you want to continue upgrading this collaborative plan?"

    ## Available Actions
    - **continue** - Proceed with upgrading the existing collaborative plan
    - **exit** - Stop the upgrade process and keep the plan unchanged

    Please select one of the keywords above.
    STOP.

    Mark "Present options to user" as completed.
    Mark "Process user decision" as in_progress.

    Handle user response:
    If response is "continue":
        Display: "Proceeding with collaborative plan upgrade..."
        Mark "Process user decision" as completed.
        Continue to plan parsing

    If response is "exit":
        Display: "Upgrade cancelled. Plan remains unchanged."
        Mark "Process user decision" as completed.
        Exit

    Execute <ValidateUserResponse/> with:
        expected_keywords: [continue, exit]
        option_descriptions: [
            "- **continue** - Proceed with upgrading the existing collaborative plan",
            "- **exit** - Stop the upgrade process and keep the plan unchanged"
        ]
</HandleExistingCollaborativePlan>
