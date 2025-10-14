# Make a Plan

Create a structured plan document based on our discussion.

**Arguments**: $ARGUMENTS (name of the new plan document to create in project root)

<Persona>
@~/.claude/shared/personas/architect_persona.md

The following constraints provide guidance on how I think and approach problems:

@~/.claude/shared/constraints/design_review_constraints.md
</Persona>

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 0:** Execute <Persona/> to adopt the Software Architect persona
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

    **STOP** and wait for user response. Do not proceed until valid input received.

    Store the response as PLAN_TYPE for use in subsequent steps.
    
    **Keyword Handling**:
    - "collaborative", "collab", "together" → PLAN_TYPE = collaborative
    - "agent", "solo", "independent" → PLAN_TYPE = agent
    - Any other response → Ask for clarification
</SelectPlanType>

## STEP 3: INTERACTIVE PLAN BUILDING

<InteractivePlanBuilding>
    **Create TodoWrite with sections to process:**
    - Create todos for each section that will be processed based on PLAN_TYPE
    - If PLAN_TYPE = collaborative: Include CollaborativeExecutionProtocol + standard sections
    - If PLAN_TYPE = agent: Include only standard sections from PlanSections
    - Mark each section as 'in_progress' when presenting to user
    - Mark as 'completed' when user makes their choice (accept/revise/skip)
    - Track PLAN_TYPE and section inclusion decisions for final document

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
    4. Present the suggestion to the user with these options:
       - **accept**: Use the suggested content as-is
       - **revise**: User provides modifications or replacement text
       - **skip**: Omit this section from the plan
       - **expand**: Request more detailed suggestions

       **Response Handling**:
       - "accept", "ok", "yes", "use it" → accept
       - "revise", "modify", "change", "edit" → revise
       - "skip", "omit", "no", "ignore" → skip
       - "expand", "more", "detail", "elaborate" → expand
       - Any other response → Ask for clarification with examples

       **Error Recovery**: If user provides invalid response:
       - Show: "Please respond with one of: accept, revise, skip, or expand"
       - After 2 invalid attempts, inform user: "Defaulting to 'skip' to continue" and proceed

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
    <Testing/>
    <RiskAssessment/>
    <SuccessCriteria/>
    <OpenQuestions/>
    <DesignReviewNotes/>
</PlanSections>

### Section Definitions

<CollaborativeExecutionProtocol>
    **Purpose**: Define collaborative implementation process with validation checkpoints
    **Only For**: COLLABORATIVE plans

    **Template**:
    ```markdown
    ## EXECUTION PROTOCOL
    For each step: DESCRIBE → AWAIT APPROVAL → IMPLEMENT → BUILD & VALIDATE → CONFIRM → PROCEED

    ## INTERACTIVE IMPLEMENTATION SEQUENCE
    ### STEP 1: [Step Name]
    **Status:** ⏳ PENDING
    **Objective:** [Clear goal]
    **Changes:** [List specific changes]
    **Files:** [List files to modify]
    **Expected outcome:** [What should work]

    ### STEP N: Final Validation
    **Status:** ⏳ PENDING
    **Validation checklist:**
    - [ ] [Specific tests]
    - [ ] All tests pass
    ```
    **Status markers:** ⏳ PENDING, 🔄 IN PROGRESS, ✅ COMPLETED, ❌ BLOCKED
</CollaborativeExecutionProtocol>

<TitleAndOverview>
    **Purpose**: Provide immediate understanding of plan's goal
    **Template**: `# [Title]` + `## Overview` with 1-3 sentence description
</TitleAndOverview>

<ProblemStatement>
    **Purpose**: Document current issues with evidence
    **Template**: List specific problems with examples, include metrics where applicable
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

<Testing>
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
</Testing>

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
