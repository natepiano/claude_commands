# Make a Plan

Create a structured plan document based on our discussion.

**Arguments**: $ARGUMENTS (name of the new plan document to create in project root)

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 1:** Execute <ShowPlanLocation/>
    **STEP 2:** Execute <SelectPlanType/>
    **STEP 3:** Execute <InteractivePlanBuilding/>
    **STEP 4:** Execute <PlanFinalization/>
</ExecutionSteps>

## STEP 1: SETUP

<ShowPlanLocation>
    Display to user:
    ```
    Creating plan document: [filename from $ARGUMENTS or "plan-[feature-name].md"]
    Location: [project root path]/[filename]
    ```
    Then immediately proceed to Step 2.
</ShowPlanLocation>

## STEP 2: SELECT PLAN TYPE

<SelectPlanType>
    Ask the user:
    ```
    What type of implementation plan would you like to create?
    
    **collaborative** - A plan with step-by-step validation checkpoints where we work together
                       (includes execution protocol for guided implementation)
    
    **agent** - A plan for independent implementation by the coder
              (traditional plan without execution protocol)
    
    Please type: collaborative or agent
    ```
    
    Store the response as PLAN_TYPE for use in subsequent steps.
    
    **Keyword Handling**:
    - "collaborative", "collab", "together" → PLAN_TYPE = collaborative
    - "agent", "solo", "independent" → PLAN_TYPE = agent
    - Any other response → Ask for clarification
</SelectPlanType>

## STEP 3: INTERACTIVE PLAN BUILDING

<InteractivePlanBuilding>
    **Context**: Since we've been discussing this feature/refactoring, use the conversation context to generate intelligent suggestions for each section.

    **IF PLAN_TYPE = collaborative**:
    - First, build the <CollaborativeExecutionProtocol/> section (see definition below)
    - Then proceed with standard sections from <PlanSections/>
    - Ensure Implementation Strategy references the execution protocol
    
    **IF PLAN_TYPE = agent**:
    - Skip <CollaborativeExecutionProtocol/>
    - Proceed with standard sections from <PlanSections/>

    For each section defined in <PlanSections/>:

    1. Present the section name and description from its tagged definition
    2. Generate suggested content based on our discussion so far
    3. **For Problem Statement section specifically**: Before presenting the suggestion, ask user:
       "Are there any specific metrics (file sizes, performance numbers, complexity measures, etc.) we should document as current state? These help establish baselines for measuring success."
    4. **For Migration Strategy section specifically**: 
       - **If PLAN_TYPE = collaborative**: Skip asking - automatically use phased template
       - **If PLAN_TYPE = agent**: Before presenting the suggestion, ask user:
         "Is this change simple enough to be implemented as one conceptual unit (Atomic), or does it require a phased approach with intermediate review points (Phased)? Complex changes affecting multiple systems typically benefit from phased implementation."
    5. Present the suggestion to the user with these options:
       - **accept**: Use the suggested content as-is
       - **revise**: User provides modifications or replacement text
       - **skip**: Omit this section from the plan
       - **expand**: Request more detailed suggestions
    6. Track which sections are included for the final document

    **CRITICAL**: Make suggestions specific and detailed based on what we've discussed. Don't generate generic placeholder content.
</InteractivePlanBuilding>

## STEP 4: FINALIZATION

<PlanFinalization>
    **IF PLAN_TYPE = collaborative**:
    1. Place <CollaborativeExecutionProtocol/> content at the TOP of the document
    2. Follow with all other accepted/revised sections
    3. Ensure Implementation Strategy references the execution protocol steps
    4. Show user a preview of the complete plan structure
    5. Ask for confirmation before writing
    6. Write the plan document to the specified location
    7. Provide summary emphasizing this is a collaborative implementation plan
    
    **IF PLAN_TYPE = agent**:
    1. Compile all accepted/revised sections into final plan document (standard flow)
    2. Show user a preview of the complete plan structure
    3. Ask for confirmation before writing
    4. Write the plan document to the specified location
    5. Provide summary of what was created
</PlanFinalization>

## PLAN STRUCTURE DEFINITIONS

<PlanSections>
    <CollaborativeExecutionProtocol/> <!-- Only for collaborative plans -->
    <TitleAndOverview/>
    <ProblemStatement/>
    <ProposedSolution/>
    <Implementation/>
    <MigrationStrategy/>
    <Testinging/>
    <RiskAssessment/>
    <SuccessCriteria/>
    <OpenQuestions/>
    <DesignReviewNotes/>
</PlanSections>

### Section Definitions

<CollaborativeExecutionProtocol>
    **Section**: Collaborative Execution Protocol
    **Purpose**: Define the step-by-step collaborative implementation process with validation checkpoints
    **Only For**: COLLABORATIVE plans (not included in agent-only plans)

    **Format**:
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
       [project-specific build commands, e.g., cargo build && cargo test]
       ```
       Then inform user to run any necessary reconnection commands

    5. **CONFIRM**: Wait for user to confirm the build succeeded

    6. **TEST** (if applicable): Run validation tests specific to that step

    7. **MARK COMPLETE**: Update this document to mark the step as ✅ COMPLETED

    8. **PROCEED**: Move to next step only after confirmation
    </Instructions>

    ## INTERACTIVE IMPLEMENTATION SEQUENCE

    ### STEP 1: [Step Name]
    **Status:** ⏳ PENDING
    
    **Objective:** [Clear goal for this step]
    
    **Changes to make:**
    1. [Specific change 1]
    2. [Specific change 2]
    
    **Files to modify:**
    - `[file path 1]`
    - `[file path 2]`
    
    **Expected outcome:**
    - [What should work after this step]
    - [How to verify success]

    ### STEP 2: [Step Name]
    **Status:** ⏳ PENDING
    
    [Similar structure...]

    ### STEP N: Final Validation
    **Status:** ⏳ PENDING
    
    **Objective:** Verify all changes work correctly
    
    **Validation checklist:**
    - [ ] [Specific test 1]
    - [ ] [Specific test 2]
    - [ ] All tests pass
    - [ ] No regressions
    
    **Expected outcome:**
    - System fully migrated/implemented
    - Ready for production use
    ```

    **Guidelines**:
    - Each step should be independently testable
    - Status markers: ⏳ PENDING, 🔄 IN PROGRESS, ✅ COMPLETED, ❌ BLOCKED
    - Include specific build/test commands for the project
    - Reference this protocol in the Implementation Strategy section
    - Steps should align with phases defined in Implementation Strategy
</CollaborativeExecutionProtocol>

<TitleAndOverview>
    **Section**: Title and Overview
    **Purpose**: Provide immediate understanding of the plan's goal

    **Format**:
    ```markdown
    # [Plan Title]

    ## Overview
    [1-3 sentence description of what this plan accomplishes]
    ```

    **Guidelines**:
    - Title should be specific and action-oriented
    - Overview must be concise but complete
    - Include the primary goal and scope
</TitleAndOverview>

<ProblemStatement>
    **Section**: Problem Statement / Current State Analysis
    **Purpose**: Document what's wrong and why change is needed

    **Format**:
    ```markdown
    ## Problem Statement

    ### Current Issues
    - [Specific problem 1 with evidence/examples]
    - [Specific problem 2 with evidence/examples]

    ### Current State Metrics (if applicable)
    - File sizes: [relevant metrics]
    - Performance: [relevant metrics]
    - Complexity: [relevant metrics]
    ```

    **Guidelines**:
    - Be specific with concrete examples from our discussion
    - Include code snippets showing problems we've identified
    - Quantify issues where possible (file sizes, performance metrics, etc.)
    - Reference actual files and line numbers we've examined
</ProblemStatement>

<ProposedSolution>
    **Section**: Proposed Solution
    **Purpose**: Describe the approach to solve the problems

    **Format**:
    ```markdown
    ## Proposed Solution

    ### Core Approach
    [High-level description of the solution]

    ### Key Design Principles
    - [Principle 1]: [explanation]
    - [Principle 2]: [explanation]

    ### Architecture Decision
    [Why this approach over alternatives]
    ```

    **Guidelines**:
    - Focus on the "what" and "why" based on our analysis
    - Include architectural approaches we've discussed
    - Explain trade-offs and decisions made in our conversation
</ProposedSolution>

<Implementation>
    **Section**: Implementation Strategy
    **Purpose**: Detail how to implement the solution

    **Format for AGENT-ONLY plans**:
    ```markdown
    ## Implementation Strategy

    ### Phase 1: [Phase Name]
    **Goal**: [What this phase accomplishes]

    #### Steps:
    1. [Specific action with code example if relevant]
    2. [Specific action with code example if relevant]

    ### Phase 2: [Phase Name]
    [Similar structure]
    ```

    **Format for COLLABORATIVE plans**:
    ```markdown
    ## Implementation Strategy

    **NOTE:** This plan follows the Collaborative Execution Protocol defined above.
    Each phase below corresponds to steps in the INTERACTIVE IMPLEMENTATION SEQUENCE.

    ### Phase 1: [Phase Name] → See STEP 1 in Execution Protocol
    **Goal**: [What this phase accomplishes]

    #### Steps:
    1. [Specific action with code example if relevant]
    2. [Specific action with code example if relevant]

    ### Phase 2: [Phase Name] → See STEP 2 in Execution Protocol
    [Similar structure with references to protocol steps]
    ```

    **Guidelines**:
    - Break into logical phases
    - Include code examples showing changes
    - Specify dependencies between phases
    - Be specific about file modifications
    - **For collaborative plans**: Each phase should map to a step in the execution protocol
</Implementation>

<MigrationStrategy>
    **Section**: Migration Strategy
    **Purpose**: Define whether this is an atomic change or requires phased implementation

    **Format for COLLABORATIVE plans**:
    ```markdown
    ## Migration Strategy

    **Migration Strategy: Phased**

    This collaborative plan uses phased implementation by design. The Collaborative Execution Protocol above defines the phase boundaries with validation checkpoints between each step.

    #### Phase Overview
    Each step in the INTERACTIVE IMPLEMENTATION SEQUENCE represents a phase with:
    - User approval before implementation
    - Build validation after changes
    - Explicit confirmation before proceeding

    #### Review Points
    Review points are built into the execution protocol at each step transition.
    ```

    **Format for AGENT plans**:
    ```markdown
    ## Migration Strategy

    **Migration Strategy: [Atomic|Phased]**

    ### [If Atomic]
    This plan represents one conceptual unit that doesn't require backwards compatibility. Even if implemented across multiple commits, we start and work on it until completion without intermediate compatibility layers.

    ### [If Phased - include the following subsections]
    
    #### Phase Overview
    [Brief description of why phased approach is needed]
    
    #### Commit Groups
    **Phase 1**: [Commit Group Name]
    - Commits: [List of related commits]
    - Goal: [What this group accomplishes]
    - Validation: [How to verify this phase works]
    
    **Phase 2**: [Commit Group Name]
    - Commits: [List of related commits]
    - Goal: [What this group accomplishes]
    - Validation: [How to verify this phase works]
    
    #### Review Points
    - After Phase 1: [What to check before proceeding]
    - After Phase 2: [What to check before proceeding]
    ```

    **Guidelines**:
    - Start with clear marker: "**Migration Strategy: Atomic**" or "**Migration Strategy: Phased**"
    - **For Collaborative plans**: Always use "Phased" - the execution protocol inherently creates phases
    - For Atomic: Keep explanation brief - emphasize conceptual unity
    - For Phased: Define clear commit groups with validation points
    - Ensure each phase can compile, build, and test independently
    - Include specific review checkpoints between phases
</MigrationStrategy>

<Testinging>
    **Section**: Testing Strategy
    **Purpose**: Define how to validate the implementation

    **Format**:
    ```markdown
    ## Testing Strategy

    ### Unit Tests
    - [Test scenario 1]: [what it validates]

    ### Integration Tests
    - [Test scenario]: [expected behavior]

    ### Validation Commands
    \`\`\`bash
    # Command to verify behavior
    [specific test command]
    # Expected output: [description]
    \`\`\`
    ```

    **Guidelines**:
    - Include specific test commands
    - Define expected outputs
    - Cover edge cases
    - Include regression tests
</Testinging>

<RiskAssessment>
    **Section**: Risk Assessment
    **Purpose**: Identify and mitigate potential problems

    **Format**:
    ```markdown
    ## Risk Assessment

    ### Risks
    1. **[Risk Name]**: [Description]
       - **Likelihood**: [High/Medium/Low]
       - **Impact**: [High/Medium/Low]
       - **Mitigation**: [How to prevent or handle]
    ```

    **Guidelines**:
    - Be honest about potential problems
    - Include technical and process risks
    - Provide concrete mitigation strategies
</RiskAssessment>

<SuccessCriteria>
    **Section**: Success Criteria
    **Purpose**: Define what "done" means

    **Format**:
    ```markdown
    ## Success Criteria

    - [ ] [Specific, measurable criterion]
    - [ ] [Specific, measurable criterion]
    - [ ] All tests pass
    - [ ] No performance regressions
    ```

    **Guidelines**:
    - Make criteria specific and testable
    - Include both functional and non-functional requirements
    - Connect to original problems
</SuccessCriteria>

<OpenQuestions>
    **Section**: Open Questions / Future Work
    **Purpose**: Document unknowns and future improvements

    **Format**:
    ```markdown
    ## Open Questions

    1. [Question]?
       - **Context**: [Why this matters]
       - **Options**: [Possible answers]

    ## Future Improvements

    - [Improvement that's out of scope for now]
    ```

    **Guidelines**:
    - Be clear about what's uncertain
    - Distinguish between blockers and nice-to-haves
    - Include investigation needed
</OpenQuestions>

<DesignReviewNotes>
    **Section**: Design Review Skip Notes
    **Purpose**: Placeholder for design review decisions (populated by design review process)
    
    **Format**:
    ```markdown
    ## Design Review Skip Notes
    
    *This section will be populated during design reviews to track skipped, rejected, or redundant suggestions.*
    ```
    
    **Guidelines**:
    - This section starts empty
    - Will be populated by the design review process
    - Provides a designated location for review decisions
    - Ensures consistency across all plan documents
</DesignReviewNotes>

## INTERACTION TEMPLATES

<SectionPresentation>
    When presenting a section to the user:

    ---
    ## Section [N of 11]: [Section Name]

    **Purpose**: [Purpose from section definition]

    ### Suggested Content:
    ```markdown
    [Generated suggestion based on context]
    ```

    ### Options:
    - **accept** - Use this content as-is
    - **revise** - Provide your own content or modifications
    - **skip** - Don't include this section
    - **expand** - Get more detailed suggestions
    ---
</SectionPresentation>

<RevisionPrompt>
    When user chooses "revise":

    Please provide your content for this section. You can:
    1. Provide completely new content
    2. Tell me what to modify in the suggestion
    3. Paste existing content you want to use
</RevisionPrompt>

<FinalPreview>
    Before writing the plan:

    # Plan Preview: [Document Name]

    ## Included Sections:
    [List of sections that will be included]

    ## Document will be created at: [path]

    Type **confirm** to create the plan document, or **revise** to make changes.
</FinalPreview>
