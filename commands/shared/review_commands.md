## MAIN WORKFLOW

<ExecutionSteps>
    **CRITICAL: You MUST use the Task tool for reviews. Do NOT review code directly.**

    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 1:** Execute the <InitialReview/> - MUST use Task tool
    **STEP 2:** Summarize the subagent's findings using <InitialReviewSummary/>
    **STEP 3:** Execute the <ReviewFollowup/> - MUST use Task tool for each finding
    **STEP 4:** Execute the <UserReview/>
</ExecutionSteps>

<PlanDocument>
  The [PLAN_DOCUMENT] is either the plan we've been working on or is in
  $ARGUMENTS if it is provided.
</PlanDocument>

## STEP 1: INITIAL REVIEW

<InitialReview>
    **1. Execute <DetermineReviewTarget/> from the specific review command**

    **2. Show user what you're reviewing:**
    # Step 1: Initial Review
    Plan Document: [PLAN_DOCUMENT]

    **3. MANDATORY: Launch Task tool (DO NOT skip this):**
    - description: "review [PLAN_DOCUMENT]" OR "review [REVIEW_TARGET]"
    - subagent_type: "general-purpose"
    - prompt: Everything in <InitialReviewPrompt> below with placeholders replaced

    **4. Wait for subagent completion before Step 2**
</InitialReview>

<InitialReviewPrompt>
    **Target:** [REVIEW_TARGET]
    **CRITICAL CONTEXT**: [REVIEW_CONTEXT]
    **WARNING**: This is a plan for FUTURE changes. Do NOT report issues about planned features not existing in current code - they don't exist because they haven't been built yet!
    **Review Constraints**: Follow these analysis principles:
    <ReviewConstraints/>

    Review [REVIEW_TARGET] using the categories defined above and provide structured findings.
    It is important that you think very hard about this review task.

    **CRITICAL ID GENERATION REQUIREMENT**: <IDGenerationRules/>

    **CRITICAL CODE IDENTIFICATION REQUIREMENT**:
    **For DESIGN/PLAN REVIEWS**: <PlanCodeIdentification/>

    **For CODE REVIEWS**:
    1. Focus on implementation quality, safety, and design issues in the code

    **CODE CONTEXT REQUIREMENT**: <CodeExtractionRequirements/>

    **CRITICAL**: Return your findings using the exact format specified in <InitialReviewJson/>
</InitialReviewPrompt>

<InitialReviewJson>
    <JsonFormatInstructions/>
Return EXACTLY this JSON structure:
```json
{
  "findings": [
    <BaseReviewJson/> // Use the base structure defined above for each finding
  ]
}
```

Additional Requirements for Initial Review:
- Sort findings array with TYPE-SYSTEM issues first
- Include "impact" field as specified in <BaseReviewJson/>
</InitialReviewJson>

<CodeIdentificationExamples>
GOOD Example - Properly identified code:
```json
{
  "id": "TYPE-SYSTEM-2",
  "title": "Boolean state flags instead of state machine types",
  "location": {
    "plan_reference": "Section: Mutation Path Builder Refactoring",
    "code_file": "mcp/src/brp_tools/mutation_path_builder.rs",
    "line_start": 312,
    "function": "build_paths",
  },
  "issue": "Plan proposes using boolean flag is_option_type in build_paths function...",
  "current_code": "[Actual code from mutation_path_builder.rs:312-328]"
}
```

BAD Example - Missing code identification:
```json
{
  "id": "TYPE-SYSTEM-2",
  "title": "Boolean state flags instead of state machine types",
  "location": "plan-wrapper-removal.md line 429",
  "issue": "Uses boolean flag is_option_type...",
  "current_code": "[Code copied from the plan document]"
}

BAD Example - Using line numbers instead of section titles:
```json
{
  "location": {
    "plan_reference": "plan-wrapper-removal.md:429-445",
    "code_file": "mcp/src/brp_tools/mutation_path_builder.rs"
  }
}
```

GOOD Example - Proper ID generation with existing findings:
If the plan already contains: DESIGN-1, DESIGN-2, DESIGN-3, TYPE-SYSTEM-1, TYPE-SYSTEM-2
Your new findings should be: DESIGN-4, DESIGN-5, TYPE-SYSTEM-3, etc.

BAD Example - Reusing existing IDs:
If the plan already contains: DESIGN-1, DESIGN-2, DESIGN-3
DO NOT generate: DESIGN-1, DESIGN-2 (even if reviewing different sections)
</CodeIdentificationExamples>

<InitialReviewSummary>

Provide a high-level summary of the subagent's findings:

# Review Summary
## Total findings: [number]
- Categories found: [list categories with counts, e.g., TYPE-SYSTEM (3), QUALITY (2)]
- Priority breakdown: [High: X, Medium: Y, Low: Z]

## Key themes:
[2-3 bullet points about main issues identified]

**Next we will proceed with a deep review followup on each finding**

</InitialReviewSummary>

## STEP 2: INVESTIGATION

<ReviewFollowup>
    **Execute Parallel Investigation of All Findings**

    Before proceeding:
    1. Review the findings from the initial review subagent
    2. Filter out any findings that no longer apply or have been addressed
    3. Group the remaining findings by category

    **Launch ALL investigations in PARALLEL using MULTIPLE Task tool calls:**
    1. **CRITICAL**: Create a SINGLE response containing ALL Task tool invocations at once
       - Do NOT send tasks one at a time
       - Do NOT wait between task launches
       - ALL Task tool calls must be in the SAME message
    2. Each Task tool call should have:
       - description: "Investigate [CATEGORY-ID]: [Brief title of finding]"
       - subagent_type: "general-purpose"
       - prompt: Use the template from <ReviewFollowupPrompt/> with the specific finding details
    3. Example: If you have 5 findings, your ONE response must contain 5 Task tool calls sent together
    4. Only AFTER sending all tasks, wait for ALL investigation subagents to complete
    5. Parse the JSON results and merge with original findings
    6. Prepare to present each investigated finding to the user in the next step

    **CRITICAL**: Do NOT present findings to the user yet - just complete the investigations and compile results.
</ReviewFollowup>

<ReviewFollowupPrompt>
    **Investigation Task for Finding [CATEGORY-ID]**

    Review the following finding from the initial review:

    **Original Finding (JSON):**
    ```json
    [Insert the complete finding object from the InitialReviewJson findings array]
    ```

    Analyze this finding and provide an investigation verdict. It is important that you think very hard about this review task.

    **Your Investigation Tasks:**
    1. Verify if this is a real issue or false positive
    2. Assess the fix complexity
    3. Consider maintenance and long-term implications
    4. Provide a verdict: [EXPECTED_VERDICTS]
    5. Include detailed reasoning for your verdict in simple, easy-to-understand terms
       - Avoid technical jargon where possible
       - Explain the "why" in plain language
       - Focus on practical impact rather than theoretical concepts
    6. **CRITICAL**: For any verdict that recommends action (CONFIRMED, FIX RECOMMENDED, MODIFIED, etc.),
       you MUST include concrete suggested_code showing the improved implementation
       - For MODIFIED verdicts: Show your alternative approach as actual code, not just a description
    7. **CRITICAL**: If the original finding has insufficient code context,
       you MUST read the file and provide the full context following <CodeExtractionRequirements/>
    8. **CRITICAL FOR PLAN REVIEWS**: If the original finding references a plan document,
       follow <PlanCodeIdentification/> requirements.
    9. **CRITICAL FOR ANY REVIEW** do the review and follow <TypeSystemPrinciples/>

    **CRITICAL RESTRICTIONS - You may ONLY:**
    - Read files to understand context
    - Analyze code structure and patterns
    - Evaluate the proposed change

    **You may NOT:**
    - Run any applications or servers (cargo run, npm start, etc.)
    - Execute blocking or interactive commands
    - Create or modify any files
    - Run tests or build commands
    - Make any changes to the codebase

    **CRITICAL**: Return your findings using the EXACT format specified in <ReviewFollowupJson/>
</ReviewFollowupPrompt>

<ReviewFollowupJson>
<JsonFormatInstructions>
Return EXACTLY this JSON structure (extending the <BaseReviewJson/> structure):
```json
{
  // All fields from <BaseReviewJson/> (keep original values unless updating)
  ...BaseReviewJson,

  // Additional fields for investigation followup:
  "verdict": "[CONFIRMED | MODIFIED | REJECTED | etc.]",
  "reasoning": "[Clear explanation of verdict decision in simple, easy-to-understand terms - avoid jargon, focus on practical impact]",
  "complexity": "[Simple | Moderate | Complex]",
  "risk": "[Low | Medium | High]",
  "alternative_approach": "[Optional - describe alternatives if applicable]"
}
```

Additional Requirements for Followup:
- Include ALL fields from the original finding (as defined in <BaseReviewJson/>)
- verdict must be one of: [EXPECTED_VERDICTS]
- alternative_approach is OPTIONAL - omit if not applicable
- For verdicts recommending action: suggested_code is REQUIRED with concrete implementation
- **CRITICAL FOR MODIFIED VERDICT**: Must include suggested_code showing the alternative approach in concrete code
- For verdicts NOT recommending action: omit suggested_code field
- suggested_code must show the actual fixed/improved code, not instructions about what to change
- current_code field MUST be expanded if original had insufficient context (minimum 5-10 lines)
</ReviewFollowupJson>

## STEP 3: USER INTERACTION

<UserOutput>
Present the investigation findings to the user using this format
(derived from the ReviewFollowupJson data).
**Note**: Use appropriate language identifier in code blocks (rust, python, javascript, etc.):

# **[id]**: [title] ([current_number] of [total_findings])
**Issue**: [issue]
**Priority**: [priority] - **Complexity**: [complexity] - **Risk**: [risk]
[if location.function exists: **Function**: [location.function]]
[if location.plan_reference exists: **Plan Reference**: [location.plan_reference]]

## Current Code
**Location**: [relative path from location.code_file]:[location.line_start]
```rust
[current_code - ONLY the actual code from the real file, not the plan's proposal]
```

## Suggested Code Change
```rust
[suggested_code - ONLY the actual code, no markdown headers or explanations - only include if verdict recommends action]
```

## Analysis
**Reasoning**: [reasoning]

**Alternative Approach**: [alternative_approach - only include if present in JSON]

## **Verdict**: [verdict]
</UserOutput>

<UserReview>
    **Present Findings to User for Interactive Review**

    **FIRST**: Create a TodoWrite list to track the review process:
    - Create one todo item for each finding with status "pending"
    - Format: "Review [id]: [title]"
    - Mark each as "in_progress" when presenting it
    - Mark as "completed" after user responds with a keyword

    **TRACK DECISIONS**: Maintain a list of all decisions made and what changes they caused.

    For each investigated finding (in order, starting with TYPE-SYSTEM):

    1. Update TodoWrite to mark current finding as "in_progress"
    2. **MANDATORY**: Apply <PriorDecisionReconciliation/> before presenting any finding
    3. Present the finding using the format from <UserOutput/> (from the investigation results)
       - Include the (n of m) counter in the title
       - Add any relevant notes about how prior decisions affect this finding
    4. Display available keywords using <KeywordPresentation/> format based on the verdict
       - **CRITICAL**: If returning from an investigation, use the UPDATED verdict from the investigation result, NOT the original verdict
    5. STOP and wait for user's response
    6. **CRITICAL USER RESPONSE HANDLING**:
       - **IF USER PROVIDES A KEYWORD**: Execute the action from your command file's <KeywordExecution/> section
       - **IF USER PROVIDES ANY OTHER RESPONSE** (discussion, alternative proposal, question, clarification):
         a. Engage with the user's input appropriately
         b. If discussion leads to agreement/resolution, summarize the agreed approach
         c. **CRITICAL**: If you take any action that modifies the plan (via Edit/Write tools):
            - Mark the current todo as "completed"
            - Ask "Edit complete. Type 'continue' to proceed to the next finding."
            - Wait for user confirmation before proceeding
            - Skip steps d-f below and continue from step 8
         d. **MANDATORY**: Present keywords using <KeywordPresentation/> format
         e. **MANDATORY**: State "Please select one of the keywords above to proceed."
         f. **DO NOT CONTINUE** to next finding until user provides a keyword
         g. If user continues discussion instead of selecting keyword, repeat steps a-f
       - **SPECIAL CASE FOR "investigate" KEYWORD**: After investigation completes,
         a. Parse the investigation JSON result to extract updated verdict and reasoning
         b. Update the finding object with new verdict, reasoning, complexity, risk, etc.
         c. Present the finding again using the same <UserOutput/> format but with updated information
         d. Present keywords appropriate for the NEW verdict (not the original verdict)
         e. Wait for user's new keyword response before proceeding
         f. Do NOT mark the todo as completed until user provides a keyword for the updated finding
    7. After keyword execution:
       - **FOR ALL EDIT ACTIONS** (agree, skip, skip with prejudice, accept as built, redundant):
         a. After executing the Edit tool to modify the plan
         b. STOP and ask "Edit complete. Type 'continue' to proceed to the next finding."
         c. Wait for user confirmation before proceeding
    8. Update TodoWrite to mark current finding as "completed" ONLY after user confirms continuation
    9. **USER CONTROL REQUIREMENTS**:
       - Wait for explicit user keyword input for all decisions
       - If user engages in discussion, always return to keyword selection
       - Stop after plan edits and wait for "continue" confirmation
       - Never auto-select keywords or assume user intentions
       - Never proceed to next finding without explicit user control
       - The user controls ALL decisions and progression

    After all findings are reviewed, provide a summary using <FinalSummary/>
</UserReview>

<ReviewFindingBaseTemplate>
    **Standard base format for all review finding updates:**

    ```
    ## [finding.id]: [finding.title] - **Verdict**: [finding.verdict] [additional_status_suffix]
    - **Status**: [ACTION_STATUS]
    - **Location**: [finding.location.plan_reference]
    - **Issue**: [finding.issue]
    - **Reasoning**: [finding.reasoning]
    [action_specific_sections]
    ```

    Where:
    - [additional_status_suffix]: Optional suffix like "✅", "- REDUNDANT", "- DEVIATION ACCEPTED"
    - [ACTION_STATUS]: "SKIPPED", "APPROVED - To be implemented", "PERMANENTLY REJECTED", etc.
    - [action_specific_sections]: Additional sections based on the specific action
</ReviewFindingBaseTemplate>

<PlanUpdateFormat>
    **CRITICAL**: When updating plan documents with review findings, use <ReviewFindingBaseTemplate/> format.

    **IMPORTANT**: Cross-references MUST use section titles, not line/section numbers
    - GOOD: "See 'Section: Type System Design' for details"
    - BAD: "See section 3.2" or "See line 429"
    - WHY: Plans constantly evolve; line numbers and section numbers become stale

    Use the appropriate template below based on the keyword action.
</PlanUpdateFormat>

<SkipTemplate>
Use <ReviewFindingBaseTemplate/> with:
- [additional_status_suffix]: (none)
- [ACTION_STATUS]: "SKIPPED"
- [action_specific_sections]: "- **Decision**: User elected to skip this recommendation"
</SkipTemplate>

<SkipWithPrejudiceTemplate>
Use <ReviewFindingBaseTemplate/> with:
- [additional_status_suffix]: (none, but prefix with "⚠️ PREJUDICE WARNING - " before finding.id)
- [ACTION_STATUS]: "PERMANENTLY REJECTED"
- [action_specific_sections]: "- **Critical Note**: DO NOT SUGGEST THIS AGAIN - Permanently rejected by user"
</SkipWithPrejudiceTemplate>

<RedundantTemplate>
Use <ReviewFindingBaseTemplate/> with:
- [additional_status_suffix]: "- REDUNDANT"
- [ACTION_STATUS]: "REDUNDANT - Already addressed in plan"
- [action_specific_sections]:
  "- **Existing Implementation**: [Quote the relevant section from the plan that already addresses this]
  - **Plan Section**: [Section title where this is already covered]
  - **Critical Note**: This functionality/design already exists in the plan - future reviewers should check for existing coverage before suggesting"
</RedundantTemplate>


<AcceptAsBuiltTemplate>
Use <ReviewFindingBaseTemplate/> with:
- [additional_status_suffix]: "- DEVIATION ACCEPTED"
- [ACTION_STATUS]: "ACCEPTED AS BUILT"
- [action_specific_sections]:
  "- **Plan Specification**: [What the plan originally specified]
  - **Actual Implementation**: [finding.current_code or description]
  - **Decision**: Implementation deviation accepted and documented"
</AcceptAsBuiltTemplate>

<KeywordPresentation>
    # Available Actions
    Look up the appropriate keywords from the <ReviewKeywords/> section in your command file
    based on the verdict, then display them using this EXACT format:

    - **[keyword]** - [description of what this action will do]

    Example for CONFIRMED verdict in design review:
    ## Available Actions
    - **agree** - Update plan document with the suggested design improvement
    - **skip** - Add to "Design Review Skip Notes" section and continue
    - **skip with prejudice** - Permanently reject with ⚠️ PREJUDICE WARNING
    - **investigate** - Launch deeper investigation of the design issue
</KeywordPresentation>

<FinalSummary>
    Review Complete!
    - Total findings reviewed: [count]
    - Actions taken: [Count each keyword from <ReviewKeywords/> that was used]
    - Categories addressed: [list with counts]
</FinalSummary>

## PRIOR DECISION RECONCILIATION

<PriorDecisionReconciliation>
**CRITICAL AUTHORITY DIRECTIVE**: Before presenting any finding, you MUST actively reconcile it against ALL prior decisions made in this review session. This is not optional.

**Your Authority and Responsibility:**
- You have FULL AUTHORITY to modify, invalidate, or completely rewrite any finding based on prior decisions
- You MUST change verdicts if prior decisions make the original verdict inappropriate
- You MUST completely rewrite reasoning when context has changed
- You MUST invalidate findings that become obsolete due to prior decisions
- You are the synthesis agent - act decisively, not passively

**Mandatory Analysis Steps:**
1. **Decision Impact Assessment**: For each prior decision, explicitly evaluate:
   - Does this decision solve the current finding? → Change verdict to OBSOLETE
   - Does this decision conflict with the current finding? → Rewrite or invalidate
   - Does this decision change the priority/risk of the current finding? → Update accordingly
   - Does this decision require changes to the suggested solution? → Rewrite suggested_code

2. **Verdict Authority**: You MUST change verdicts when warranted:
   - CONFIRMED → OBSOLETE (if prior decision already addresses this)
   - CONFIRMED → MODIFIED (if prior decision requires different approach)
   - CONFIRMED → REJECTED (if prior decision makes this inappropriate)
   - Any verdict → Any other verdict based on new context

3. **Reasoning Rewrite**: When prior decisions change context:
   - COMPLETELY rewrite the reasoning section to reflect current reality
   - Reference specific prior decisions and their implications
   - Explain how the finding has been affected by previous choices
   - Do NOT just "note" changes - fully integrate them into new reasoning

4. **Finding Synthesis**: Look for cross-finding implications:
   - Do multiple findings become redundant after prior decisions?
   - Are there new issues created by the combination of prior decisions?
   - Should findings be merged or split based on evolving context?

**Examples of Decisive Action:**
- "Due to prior decision to implement StateEnum approach in DESIGN-2, this finding is now OBSOLETE - the type safety issue is already resolved."
- "Given the user's rejection of the wrapper removal in TYPE-SYSTEM-1, this finding's verdict changes from CONFIRMED to MODIFIED - we must work within the existing wrapper structure."
- "Prior decisions to use enum-based validation make this finding's suggested approach incompatible. Verdict changed to REJECTED with new alternative approach."

**Language Requirements:**
- Use decisive language: "This finding is now...", "Verdict changed to...", "Due to prior decision..."
- Avoid passive language: "may be affected", "could potentially", "might need adjustment"
- State changes authoritatively, not tentatively
- Take ownership of the synthesis process

**When to Skip a Finding Entirely:**
If a finding becomes completely irrelevant due to prior decisions, mark it as completed in TodoWrite and skip to the next finding. Document the skip decision in your tracking.
</PriorDecisionReconciliation>

## SHARED DEFINITIONS
Reusable components and definitions used throughout the review workflow

<BaseReviewJson>
Base JSON structure for review findings:
```json
{
  "id": "[CATEGORY]-[NUMBER]",
  "category": "[CATEGORY]",
  "title": "[Brief descriptive title]",
  "location": {
    "plan_reference": "[If reviewing a plan: section title, NOT line numbers - e.g., 'Section: Mutation Path Implementation']",
    "code_file": "[Relative path to actual code file from project root]",
    "line_start": [number in code file],
    "function": "[Function/method name if applicable]",
  },
  "issue": "[Specific problem description]",
  "current_code": "[Code snippet or text showing the issue]",
  "suggested_code": "[Improved version or recommendation]",
  "priority": "[High/Medium/Low]",
  "impact": "[Why this matters]"
}
```

Base Requirements:
- [CATEGORY] must be from <ReviewCategories/> (e.g., TYPE-SYSTEM, QUALITY, DESIGN)
- [NUMBER] must follow <IDGenerationRules/>
- TYPE-SYSTEM issues always have priority: "High"
- For plan/design reviews: location MUST include both plan_reference AND code_file
- current_code must be from the ACTUAL code file, not copied from the plan
- If code file cannot be identified, set location.code_file to "UNKNOWN - NEEDS INVESTIGATION"
- current_code must follow <CodeExtractionRequirements/>
- current_code must be PURE CODE ONLY - no markdown headers, instructions, or prose
</BaseReviewJson>

<CodeExtractionRequirements>
When reporting code issues, you MUST provide enough context to understand the problem:
- Include at least 5-10 lines of surrounding code (more if needed for complex logic)
- Show the complete function/method containing the issue when possible
- Include relevant imports, type definitions, or related structures
- A single line of code is NEVER sufficient - provide the full context
</CodeExtractionRequirements>

<IDGenerationRules>
When generating IDs for review findings:
1. FIRST, scan the entire plan document for existing review IDs (e.g., DESIGN-1, TYPE-SYSTEM-3)
2. Track the highest number used for each category
3. Generate NEW IDs starting from the next available number
4. Each category tracks its own sequence independently
5. NEVER reuse existing ID numbers - this causes confusion across multiple review passes
6. Even if the original finding was rejected, do not reuse its number

Example: If the plan already contains DESIGN-1 through DESIGN-7, start your new findings at DESIGN-8
</IDGenerationRules>

<JsonFormatInstructions>
**CRITICAL**: Return results as JSON. Do NOT use markdown formatting or narrative text.
Return ONLY the JSON object, no additional text.
</JsonFormatInstructions>

## REVIEW CONSTRAINTS
Constraints to be included in specific review scenarios (referenced by design_review, code_review, alignment_review)

<TypeSystemPrinciples>
Follow these type system design principles as highest priority:
1. **Conditional Audit**: Every if-else chain is a potential design failure - look for string-based conditionals that could be replaced with enums and pattern matching
2. **Function vs Method Audit**: Every standalone utility function is suspect - functions that should be methods on a type that owns the behavior
3. **String Typing Violations**: Every string representing finite values should be an enum (exceptions: format validation, arbitrary text processing)
4. **State Machine Failures**: State tracking with primitives instead of types - boolean flags that should be part of state enums
5. **Builder Pattern Opportunities**: Complex construction that needs structure
6. **No Magic Values**: Never allow magic values - use enums that can serialize - ideally with conversion traits for ease of use. If an enum is not appropriate, a constant should be used.
</TypeSystemPrinciples>

<SkipNotesCheck>
**MANDATORY** DO THIS FIRST
Check for a "Design Review Skip Notes" section in the document:
1. Read every single skipped item to understand rejection reasons
2. Cross-reference your ideas against previously rejected concepts
3. Do not re-suggest items marked with "⚠️ PREJUDICE WARNING"
4. Only proceed after confirming recommendations don't duplicate rejected items
</SkipNotesCheck>

<DocumentComprehension>
For plan document reviews:
1. Read the entire plan from beginning to end before making recommendations
2. Search for existing solutions before claiming something is missing
3. Quote specific sections when claiming gaps exist
4. Cross-reference sections as many topics span multiple areas
5. For every "missing" claim, either quote the section that should contain it or explain why existing content is insufficient
</DocumentComprehension>

<DesignConsistency>
For design document reviews:
1. **Internal Consistency**: Verify that all sections of the plan are consistent with each other
2. **Decision Alignment**: Check that design decisions in one section don't contradict decisions in another
3. **Terminology Consistency**: Ensure the same terms are used consistently throughout
4. **Architectural Coherence**: Verify that the overall architecture remains coherent across all sections
5. **Example Consistency**: Ensure code examples align with the described approach
6. **Flag Inconsistencies**: Report when a change in one section would require updates to other sections
</DesignConsistency>

<AtomicChangeRequirement>
**MIGRATION STRATEGY COMPLIANCE**: Check the plan document for a Migration Strategy marker:

- If you find "**Migration Strategy: Atomic**": Plans must represent complete, indivisible changes. Reject any suggestions for incremental rollouts, backward compatibility, gradual migrations, or hybrid approaches. Either keep current design unchanged OR replace it entirely - no middle ground.

- If you find "**Migration Strategy: Phased**": The plan has explicitly chosen a phased approach. Validate that the phased implementation makes sense and provides appropriate review points and validation steps between phases.

- If neither marker is present: **Default to Atomic** - Apply the atomic change requirements above.

**No Hybrid Approaches**: Do not suggest mixing atomic and phased strategies within the same plan. The migration strategy choice applies to the entire plan.
</AtomicChangeRequirement>

<DuplicationPrevention>
**MANDATORY DUPLICATION DETECTION AND ELIMINATION FOR PLAN DOCUMENTS**:

1. **Types of Duplication to Detect**:

   a) **Existing Duplication** - Already present in the code area being modified
      - Multiple functions doing the same thing
      - Repeated logic across methods
      - The plan should consolidate these, not perpetuate them

   b) **Plan-Introduced Identical** - Plan creates exact copies
      - New function that duplicates an existing function
      - Copy-pasted logic with minor variations
      - Redundant data structures or types

   c) **Plan-Introduced Overlap** - Plan creates parallel/competing paths
      - New call flow that overlaps with existing flow
      - Alternative way to accomplish same goal
      - Multiple entry points for same functionality

   d) **Pattern Duplication** - Plan inconsistently reimplements patterns
      - Error handling done differently than existing patterns
      - Validation logic that doesn't follow established approach
      - State management that conflicts with existing patterns

2. **Resolution Requirements**:
   - If ANY duplication is detected, the plan MUST be redesigned
   - No "letting them coexist" - eliminate the duplication
   - Choose ONE approach: enhance existing OR fully replace
   - Create a single source of truth

3. **Priority**:
   - All duplication issues are HIGH priority
   - Duplication compounds over time into technical debt
   - Prevention now saves major refactoring later
</DuplicationPrevention>

<CodeDuplicationDetection>
**MANDATORY CODE DUPLICATION DETECTION FOR CODE REVIEWS**:

1. **Types of Code Duplication to Detect**:

   a) **Identical Functions** - Multiple functions with same or nearly identical implementation
      - Copy-pasted functions with minor parameter differences
      - Functions that could be generalized with parameters
      - Utility functions scattered across modules

   b) **Logic Block Duplication** - Repeated code patterns within or across functions
      - Same validation logic in multiple places
      - Identical error handling blocks
      - Repeated data transformation patterns

   c) **Type/Structure Duplication** - Redundant data structures or types
      - Multiple structs representing the same concept
      - Enums with overlapping variants
      - Traits that duplicate behavior

   d) **Pattern Inconsistency** - Same functionality implemented different ways
      - Multiple approaches to same problem in the codebase
      - Inconsistent error handling strategies
      - Different state management patterns for similar use cases

2. **Resolution Requirements**:
   - If ANY duplication is detected, recommend consolidation
   - Extract common functionality into shared utilities
   - Choose ONE canonical implementation approach
   - Remove or refactor duplicate code paths

3. **Priority**:
   - All code duplication issues are HIGH priority
   - Code duplication creates maintenance burden
   - Inconsistent patterns confuse developers and create bugs
</CodeDuplicationDetection>

<PlanDocumentAnalysis>
For plan alignment reviews:
1. **Structured Reading**: Parse plan document section by section
2. **Feature Extraction**: List all promised features, APIs, and behaviors
3. **Specification Capture**: Note all technical requirements and constraints
4. **Scope Boundaries**: Identify what's explicitly excluded or marked as future work
</PlanDocumentAnalysis>

<ImplementationMapping>
For plan alignment reviews:
1. Search the codebase for each planned feature
2. Read the actual implementation files
3. Compare implementation approach against planned approach
4. Document what exists vs what was promised
5. Identify additions beyond the original plan scope
</ImplementationMapping>

<PlanNotImplementation>
**CRITICAL - THIS IS A PLAN REVIEW, NOT A CODE AUDIT**:
The plan describes FUTURE changes that haven't been implemented yet.

NEVER report as issues:
- "Proposed types/functions don't exist in codebase"
- "Current code doesn't match the plan"
- "Planned changes haven't been made"

ONLY evaluate:
- Is the plan internally consistent?
- Are the proposed changes well-designed?
- Will the plan achieve its stated goals?
- Are there better approaches?
</PlanNotImplementation>

<PlanCodeIdentification>
When reviewing plan documents:
1. Identify which actual code file(s) the plan discusses
2. Read those files to understand current implementation
3. Extract specific line numbers function names for JSON location fields
4. Compare current code with proposed changes
5. Report findings with references to both plan location and code location
6. If code file cannot be identified, mark as "NEEDS_INVESTIGATION"
</PlanCodeIdentification>
