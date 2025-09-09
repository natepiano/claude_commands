# Make a Plan

Create a structured plan document based on our discussion.

**Arguments**: $ARGUMENTS (name of the new plan document to create in project root)

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 1:** Execute <ShowPlanLocation/>
    **STEP 2:** Execute <InteractivePlanBuilding/>
    **STEP 3:** Execute <PlanFinalization/>
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

## STEP 2: INTERACTIVE PLAN BUILDING

<InteractivePlanBuilding>
    **Context**: Since we've been discussing this feature/refactoring, use the conversation context to generate intelligent suggestions for each section.

    For each section defined in <PlanSections/>:

    1. Present the section name and description from its tagged definition
    2. Generate suggested content based on our discussion so far
    3. **For Problem Statement section specifically**: Before presenting the suggestion, ask user:
       "Are there any specific metrics (file sizes, performance numbers, complexity measures, etc.) we should document as current state? These help establish baselines for measuring success."
    4. **For Migration Strategy section specifically**: Before presenting the suggestion, ask user:
       "Is this change simple enough to be implemented as one conceptual unit (Atomic), or does it require a phased approach with intermediate review points (Phased)? Complex changes affecting multiple systems typically benefit from phased implementation."
    5. Present the suggestion to the user with these options:
       - **accept**: Use the suggested content as-is
       - **revise**: User provides modifications or replacement text
       - **skip**: Omit this section from the plan
       - **expand**: Request more detailed suggestions
    6. Track which sections are included for the final document

    **CRITICAL**: Make suggestions specific and detailed based on what we've discussed. Don't generate generic placeholder content.
</InteractivePlanBuilding>

## STEP 3: FINALIZATION

<PlanFinalization>
    1. Compile all accepted/revised sections into final plan document
    2. Show user a preview of the complete plan structure
    3. Ask for confirmation before writing
    4. Write the plan document to the specified location
    5. Provide summary of what was created
</PlanFinalization>

## PLAN STRUCTURE DEFINITIONS

<PlanSections>
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

    **Format**:
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

    **Guidelines**:
    - Break into logical phases
    - Include code examples showing changes
    - Specify dependencies between phases
    - Be specific about file modifications
</Implementation>

<MigrationStrategy>
    **Section**: Migration Strategy
    **Purpose**: Define whether this is an atomic change or requires phased implementation

    **Format**:
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
