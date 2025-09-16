# Upgrade Plan to Collaborative Mode

Convert an existing plan document into a collaborative mode plan with step-by-step execution protocol.

**Arguments**: $ARGUMENTS (path to existing plan document to upgrade)

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    It's import to think harder about all steps!

    **STEP 1:** Execute <AnalyzeAndSequence/>
    **STEP 2:** Execute <ReviewAndConfirm/>
    **STEP 3:** Execute <GenerateCollaborativePlan/>
</ExecutionSteps>

## STEP 1: ANALYZE AND SEQUENCE

<AnalyzeAndSequence>

    Read the plan document from $ARGUMENTS.

    If file doesn't exist:
        Display error: "Plan file not found: $ARGUMENTS"
        Exit

    If "## EXECUTION PROTOCOL" already exists:
        Display: "This plan is already in collaborative mode."
        Exit

    Parse the plan to extract:
    - All implementation tasks and phases
    - All files to be created/modified
    - Build and test commands
    - Dependencies between changes

    Analyze dependencies and create optimal build sequence:
    1. Group related changes that should be tested together
    2. Order by compilation/build dependencies
    3. Ensure each step can be independently validated
    4. Minimize context switching between components

    Create PROPOSED_SEQUENCE with the execution steps.
    Proceed to Step 2.
</AnalyzeAndSequence>

## STEP 2: REVIEW AND CONFIRM

<ReviewAndConfirm>
    Display the proposed collaborative structure:

    ```
    Plan Upgrade Analysis: [filename]
    =====================================

    Current Structure:
    - [X] implementation phases found
    - [Y] files to be modified
    - Migration type: [Atomic/Phased]

    Proposed Collaborative Execution Sequence:

    Step 1: [Name]
      Tasks: [brief description]
      Files: [file1, file2]
      Build: [build command]

    Step 2: [Name]
      Tasks: [brief description]
      Files: [file3, file4]
      Build: [build command]
      Dependencies: Requires Step 1

    [... continue for all steps ...]

    Final Step: Complete Validation
      - Run all tests
      - Verify integration
      - Check success criteria
    ```

    Ask user:
    ```
    Does this execution sequence look correct?

    - **approve** - Generate the collaborative plan
    - **adjust** - Describe what needs to change
    - **abort** - Cancel the upgrade
    ```

    If adjust:
        Ask: "What needs to be changed in the sequence?"
        Update PROPOSED_SEQUENCE based on feedback
        Return to display and ask again

    If abort:
        Exit without changes

    If approve:
        Proceed to Step 3
</ReviewAndConfirm>

## STEP 3: GENERATE COLLABORATIVE PLAN

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

       6. **MARK COMPLETE**: Update this document to mark the step as ✅ COMPLETED

       7. **PROCEED**: Move to next step only after confirmation
       </Instructions>

       <ExecuteImplementation>
           Find the next ⏳ PENDING step in the INTERACTIVE IMPLEMENTATION SEQUENCE below.

           For the current step:
           1. Follow the <Instructions/> above for executing the step
           2. When step is complete, use Edit tool to mark it as ✅ COMPLETED
           3. Continue to next PENDING step

           If all steps are COMPLETED:
               Display: "✅ Implementation complete! All steps have been executed."
       </ExecuteImplementation>

       ## INTERACTIVE IMPLEMENTATION SEQUENCE
       ```

       For each step in PROPOSED_SEQUENCE:
         * Create STEP entry with objective, changes, files, build commands
         * Include ⏳ PENDING status marker

       Add final validation step

    2. Restructure the original plan:
       - Place EXECUTION_PROTOCOL at the top (making it executable)
       - Keep all original sections
       - Update or add Migration Strategy section:
         * Set to "**Migration Strategy: Phased**"
         * Add note: "This collaborative plan uses phased implementation by design. The Collaborative Execution Protocol above defines the phase boundaries with validation checkpoints between each step."
       - Update Implementation Strategy to reference protocol steps
       - Add Design Review Skip Notes section if missing

    3. Save the upgraded plan:
       Ask: "Save as: **[original-name]-collaborative.md** or **overwrite** original?"

       If overwrite:
           Write to $ARGUMENTS
           Display: "✅ Plan upgraded to collaborative mode: $ARGUMENTS"
           Display: "📝 The plan is now executable - run it to start implementation"

       If save-as:
           Write to suggested filename (or user-provided name)
           Display: "✅ Collaborative plan created: [filename]"
           Display: "📝 The plan is now executable - run it to start implementation"

    Display final summary:
    ```
    Upgrade Complete
    ----------------
    ✓ Created [N] collaborative execution steps
    ✓ Organized [X] tasks into buildable chunks
    ✓ Each step includes validation commands
    ✓ Plan is now executable as a command

    To start implementation, run: @[filename]
    ```
</GenerateCollaborativePlan>
