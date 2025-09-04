## MAIN WORKFLOW

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 1:** Execute the <InitialReview/>
    **STEP 2:** Summarize the subagent's findings using <InitialReviewSummary/>
    **STEP 3:** Execute the <ReviewFollowup/>
    **STEP 4:** Execute the <UserReview/>
</ExecutionSteps>

<PlanDocument>
  The [PLAN_DOCUMENT] is either the plan we've been working on or is in
  $ARGUMENTS if it is provided.
</PlanDocument>

## STEP 1: INITIAL REVIEW

<InitialReview>

    Before using the Task tool:
  - Look at the <ReviewContext/> section to understand what you're reviewing
  - If there is a [PLAN_DOCUMENT] tell the user that we are going to review it.
  - If there is not a plan document then tell the user what is the [REVIEW_TARGET] from <ReviewContext/>
  - Summarize the [REVIEW_CONTEXT] for the user.
  - Construct a [TASK_DESCRIPTION] - if there is a [PLAN_DOCUMENT] then use "review [PLAN_DOCUMENT]" otherwise use "review [REVIEW_TARGET]"

  Then use the Task tool with EXACTLY these parameters:
    - description: [TASK_DESCRIPTION]
    - subagent_type: "general-purpose"
    - prompt: The ENTIRE content between <InitialReviewPrompt> and </InitialReviewPrompt> tags below, replacing any placeholders with their actual values from <ReviewContext/> tag. And also substituting any tags that are referenced so that the full text of <ReviewConstraints/> are included.

    Wait for the subagent to complete before proceeding to Step 2.

</InitialReview>

<InitialReviewPrompt>
    **Target:** [REVIEW_TARGET]
    **CRITICAL CONTEXT**: [REVIEW_CONTEXT]
    **Review Constraints**: Follow these analysis principles:
    <ReviewConstraints/>

    Review [REVIEW_TARGET] using the categories defined above and provide structured findings.
    It is important that you think very hard about this review task.

    **CRITICAL ID GENERATION REQUIREMENT**:
    When reviewing a plan document that already contains review findings:
    1. FIRST, scan the entire plan document for existing review IDs (e.g., DESIGN-1, TYPE-SYSTEM-3)
    2. Track the highest number used for each category
    3. Generate NEW IDs starting from the next available number
    4. NEVER reuse existing ID numbers - this causes confusion across multiple review passes
    
    Example: If the plan already contains DESIGN-1 through DESIGN-7, start your new findings at DESIGN-8
    
    **CRITICAL CODE IDENTIFICATION REQUIREMENT**: 
    When reviewing plan documents or design specifications:
    1. You MUST identify which actual code file(s) the plan is discussing
    2. You MUST read those files to understand the current implementation
    3. You MUST specify the exact module, struct, function being modified
    4. Never report issues about plan proposals without investigating the actual code
    5. If you cannot identify the target code file, mark the finding as "NEEDS_INVESTIGATION"

    Example: If a plan discusses "modifying build_paths in mutation path builder", you must:
    - Find and read src/brp_tools/mutation_path_builder.rs
    - Locate the build_paths function
    - Compare current implementation with proposed changes
    - Report the finding with references to BOTH the plan location AND the actual code location

    **CODE CONTEXT REQUIREMENT**: When reporting code issues, you MUST provide enough context to understand the problem:
    - Include at least 5-10 lines of surrounding code (more if needed for complex logic)
    - Show the complete function/method containing the issue when possible
    - Include relevant imports, type definitions, or related structures
    - A single line of code is NEVER sufficient - provide the full context

    **CRITICAL**: Return your findings using the exact format specified in <InitialReviewJson/>
</InitialReviewPrompt>

<InitialReviewJson>
**CRITICAL**: Return findings as JSON. Do NOT use markdown formatting or narrative text.

Return EXACTLY this JSON structure:
```json
{
  "findings": [
    {
      "id": "[CATEGORY]-[NUMBER]",
      "category": "[CATEGORY]",
      "title": "[Brief descriptive title]",
      "location": {
        "plan_reference": "[If reviewing a plan: section title, NOT line numbers - e.g., 'Section: Mutation Path Implementation']",
        "code_file": "[Relative path to actual code file from project root]",
        "line_start": [number in code file],
        "line_end": [number in code file],
        "function": "[Function/method name if applicable]",
        "module": "[Module/struct/trait name if applicable]"
      },
      "issue": "[Specific problem description]",
      "current_code": "[Code snippet or text showing the issue]",
      "suggested_code": "[Improved version or recommendation]",
      "priority": "[High/Medium/Low]",
      "impact": "[Why this matters]"
    }
  ]
}
```

Requirements:
- [CATEGORY] must be from <ReviewCategories/> (e.g., TYPE-SYSTEM, QUALITY, DESIGN)
- [NUMBER] must NOT conflict with existing IDs in the plan document
  - Check existing findings first (e.g., if DESIGN-1 through DESIGN-7 exist, start at DESIGN-8)
  - Each category tracks its own sequence
  - NEVER reuse numbers even if the original finding was rejected
- TYPE-SYSTEM issues always have priority: "High"
- Sort findings array with TYPE-SYSTEM issues first
- For plan/design reviews: location MUST include both plan_reference AND code_file
- current_code must be from the ACTUAL code file, not copied from the plan
- If code file cannot be identified, set location.code_file to "UNKNOWN - NEEDS INVESTIGATION"
- current_code MUST contain enough context (5-10+ lines, complete functions when relevant)
- current_code must be PURE CODE ONLY - no markdown headers, instructions, or prose
- Return ONLY the JSON object, no additional text
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
    "line_end": 328,
    "function": "build_paths",
    "module": "impl MutationPathBuilder"
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

**CRITICAL** Do not output anyting else after this summary as we will next be doing a review followup.

</InitialReviewSummary>

## STEP 2: INVESTIGATION

<ReviewFollowup>
    **Execute Parallel Investigation of All Findings**

    Before proceeding:
    1. Review the findings from the initial review subagent
    2. Filter out any findings that no longer apply or have been addressed
    3. Group the remaining findings by category

    **Launch ALL investigations in ONE MESSAGE with MULTIPLE Task tool calls:**
    1. Create a SINGLE response containing ALL Task tool invocations simultaneously
    2. Each Task tool call should have:
       - description: "Investigate [CATEGORY-ID]: [Brief title of finding]"
       - subagent_type: "general-purpose"
       - prompt: Use the template from <ReviewFollowupPrompt/> with the specific finding details
    3. Example: If you have 5 findings, your ONE response must contain 5 Task tool calls
    4. Wait for ALL investigation subagents to complete (they will return ReviewFollowupJson)
    3. Parse the JSON results and merge with original findings
    4. Prepare to present each investigated finding to the user in the next step

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
    2. Assess the practical impact and fix complexity
    3. Consider maintenance and long-term implications
    4. Provide a verdict: [EXPECTED_VERDICTS]
    5. Include detailed reasoning for your verdict
    6. **CRITICAL**: For any verdict that recommends action (CONFIRMED, FIX RECOMMENDED, MODIFIED, etc.),
       you MUST include concrete suggested_code showing the improved implementation
       - For MODIFIED verdicts: Show your alternative approach as actual code, not just a description
    7. **CRITICAL**: If the original finding has insufficient code context (less than 5 lines),
       you MUST read the file and provide the full context in your response
    8. **CRITICAL FOR PLAN REVIEWS**: If the original finding references a plan document,
       you MUST investigate the actual code file to provide full context:
       - Read the code file mentioned in location.code_file
       - Show the actual current implementation (not just the plan's proposal)
       - Verify if the concern applies to the real code
       - Update location details with accurate line numbers from the actual file

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
**CRITICAL**: Return investigation results as JSON. Do NOT use markdown formatting or narrative text.

Return EXACTLY this JSON structure (extending the original finding):
```json
{
  "id": "[CATEGORY]-[NUMBER from original]",
  "category": "[CATEGORY from original]",
  "title": "[Brief descriptive title from original]",
  "location": {
    "plan_reference": "[from original if present]",
    "code_file": "[from original]",
    "line_start": [from original],
    "line_end": [from original],
    "function": "[from original if present]",
    "module": "[from original if present]"
  },
  "issue": "[Specific problem description from original]",
  "current_code": "[Code snippet from original - or updated if you read the actual file]",
  "suggested_code": "[Updated suggested code - may differ from original if MODIFIED verdict]",
  "priority": "[High/Medium/Low from original]",
  "impact": "[Why this matters from original]",
  "verdict": "[CONFIRMED | MODIFIED | REJECTED | etc.]",
  "reasoning": "[Clear explanation of verdict decision]",
  "complexity": "[Simple | Moderate | Complex]",
  "risk": "[Low | Medium | High]",
  "alternative_approach": "[Optional - describe alternatives if applicable]"
}
```

Requirements:
- Include ALL fields from the original finding
- verdict must be one of: [EXPECTED_VERDICTS]
- alternative_approach is OPTIONAL - omit if not applicable
- For verdicts recommending action: suggested_code is REQUIRED with concrete implementation
- **CRITICAL FOR MODIFIED VERDICT**: Must include suggested_code showing the alternative approach in concrete code
- For verdicts NOT recommending action: omit suggested_code field
- suggested_code must show the actual fixed/improved code, not instructions about what to change
- current_code field MUST be expanded if original had insufficient context (minimum 5-10 lines)
- current_code must be PURE CODE ONLY - no markdown headers, instructions, or prose
- Return ONLY the JSON object, no additional text
</ReviewFollowupJson>

## STEP 3: USER INTERACTION

<UserOutput>
Present the investigation findings to the user using this format
(derived from the ReviewFollowupJson data).
**Note**: Use appropriate language identifier in code blocks (rust, python, javascript, etc.):

# **[id]**: [title] ([current_number] of [total_findings]) - **Priority**: [priority] - **Complexity**: [complexity] - **Risk**: [risk]
**Issue**: [issue]
**Location**: [relative path from location.code_file]:[location.line_start]-[location.line_end]
[if location.function exists: **Function**: [location.function]]
[if location.module exists: **Module**: [location.module]]
[if location.plan_reference exists: **Plan Reference**: [location.plan_reference]]
**Impact**: [impact]

## Current Code
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

    **CRITICAL: USER CONTROL REQUIREMENT**:
    - NEVER auto-select keywords or make decisions on the user's behalf
    - NEVER assume what the user wants to do with a finding
    - ALWAYS wait for explicit user keyword input
    - ALWAYS stop after plan edits and wait for "continue" confirmation
    - The user is in control of ALL decisions

    **FIRST**: Create a TodoWrite list to track the review process:
    - Create one todo item for each finding with status "pending"
    - Format: "Review [id]: [title]"
    - Mark each as "in_progress" when presenting it
    - Mark as "completed" after user responds with a keyword

    **TRACK DECISIONS**: Maintain a list of all decisions made and what changes they caused.

    For each investigated finding (in order, starting with TYPE-SYSTEM):

    1. Update TodoWrite to mark current finding as "in_progress"
    2. **ADJUST FOR CONTEXT**: If prior decisions affect this finding, modify the presentation:
       - Update the current_code if it was changed by a prior fix
       - Note if the issue may already be addressed by a prior decision
       - Adjust the suggested_code if it needs to account for prior changes
    3. Present the finding using the format from <UserOutput/> (from the investigation results)
       - Include the (n of m) counter in the title
       - Add any relevant notes about how prior decisions affect this finding
    4. Display available keywords using <KeywordPresentation/> format based on the verdict:
       - Identify the verdict from investigation (e.g., CONFIRMED, FIX RECOMMENDED)
       - Look in your command file's <ReviewKeywords/> section for that specific verdict
       - Present ONLY the keywords listed for that verdict
       - Use the exact descriptions provided in <ReviewKeywords/>
       - **CRITICAL**: If returning from an investigation, use the UPDATED verdict from the investigation result, NOT the original verdict
    5. STOP and wait for user's keyword response
    6. Execute the action from your command file's <KeywordExecution/> section
       - **SPECIAL CASE FOR "investigate" KEYWORD**: After investigation completes,
         a. Parse the investigation JSON result to extract updated verdict and reasoning
         b. Update the finding object with new verdict, reasoning, complexity, risk, etc.
         c. **MANDATORY**: Present the finding again using the same <UserOutput/> format but with updated information
         d. **MANDATORY**: Present keywords appropriate for the NEW verdict (not the original verdict)
         e. **MANDATORY**: Wait for user's new keyword response before proceeding
         f. Do NOT mark the todo as completed until user provides a keyword for the updated finding
       - **CRITICAL FOR ALL EDIT ACTIONS** (agree, skip, skip with prejudice, accept as built):
         a. After executing the Edit tool to modify the plan
         b. **MANDATORY**: STOP and ask "Edit complete. Type 'continue' to proceed to the next finding."
         c. **MANDATORY**: Wait for user confirmation before proceeding
         d. Do NOT automatically continue to the next finding
    7. Update TodoWrite to mark current finding as "completed" ONLY after user confirms continuation
    8. **CRITICAL**: After user types "continue", THEN proceed to next finding
       - Do NOT assume user wants to continue
       - Do NOT auto-select keywords based on your judgment
       - ALWAYS wait for explicit user input

    After all findings are reviewed, provide a summary using <FinalSummary/>
</UserReview>

<PlanUpdateFormat>
    **CRITICAL**: When updating plan documents with review findings, you MUST include:
    - The finding ID and title
    - The category
    - The location where the issue was found (use section titles, NEVER line numbers)
    - The actual issue description
    - The verdict from the investigation
    - The reasoning from the investigation verdict
    - Any relevant code or suggested changes

    **IMPORTANT**: Cross-references MUST use section titles, not line/section numbers
    - GOOD: "See 'Section: Type System Design' for details"
    - BAD: "See section 3.2" or "See line 429"
    - WHY: Plans constantly evolve; line numbers and section numbers become stale

    Use the appropriate template below based on the keyword action.
</PlanUpdateFormat>

<SkipTemplate>
### [id]: [title]
- **Status**: SKIPPED
- **Category**: [category]
- **Location**: [Use section title, not line numbers - e.g., "Section: Type System Design" not "line 429"]
- **Issue**: [issue from finding]
- **Proposed Change**: [Brief summary of suggested_code if available]
- **Verdict**: [verdict from investigation]
- **Reasoning**: [reasoning from investigation]
- **Decision**: User elected to skip this recommendation
</SkipTemplate>

<SkipWithPrejudiceTemplate>
### ⚠️ PREJUDICE WARNING - [id]: [title]
- **Status**: PERMANENTLY REJECTED
- **Category**: [category]
- **Location**: [Use section title, not line numbers]
- **Issue**: [issue from finding]
- **Verdict**: [verdict from investigation]
- **Reasoning**: [reasoning from investigation]
- **Critical Note**: DO NOT SUGGEST THIS AGAIN - Permanently rejected by user
</SkipWithPrejudiceTemplate>

<AgreeTemplate>
## [id]: [title] ✅
- **Category**: [category]
- **Status**: APPROVED - To be implemented
- **Location**: [Use section title for plan references, not line numbers]
- **Issue Identified**: [issue from finding]
- **Verdict**: [verdict from investigation]
- **Reasoning**: [reasoning from investigation]

### Approved Change:
[suggested_code or description from finding]

### Implementation Notes:
[Any additional context about how this should be implemented]
</AgreeTemplate>

<AcceptAsBuiltTemplate>
## Deviation from Plan: [id] - [title]
- **Category**: [category]
- **Status**: ACCEPTED AS BUILT
- **Plan Section**: [Section title where this was specified in the plan]
- **Code Location**: [location in code]
- **Plan Specification**: [What the plan originally specified]
- **Actual Implementation**: [current_code or description]
- **Verdict**: [verdict from investigation]
- **Reasoning**: [reasoning from investigation]
- **Decision**: Implementation deviation accepted and documented
</AcceptAsBuiltTemplate>

<KeywordPresentation>
    # Available Actions
    Look up the appropriate keywords from the <ReviewKeywords/> section in your command file
    based on the verdict, then display them using this EXACT format:

    **[keyword]** - [description of what this action will do]

    Example for CONFIRMED verdict in design review:
    ## Available Actions
    **agree** - Update plan document with the suggested design improvement
    **skip** - Add to "Design Review Skip Notes" section and continue
    **skip with prejudice** - Permanently reject with ⚠️ PREJUDICE WARNING
    **investigate** - Launch deeper investigation of the design issue
</KeywordPresentation>

<FinalSummary>
    Review Complete!
    - Total findings reviewed: [count]
    - Actions taken: [Count each keyword from <ReviewKeywords/> that was used]
    - Categories addressed: [list with counts]
</FinalSummary>

## REVIEW CONSTRAINTS
following tags are about review constraints to be included in various review scenarios

<TypeSystemPrinciples>
Follow these type system design principles as highest priority:
1. **Conditional Audit**: Every if-else chain is a potential design failure - look for string-based conditionals that could be replaced with enums and pattern matching
2. **Function vs Method Audit**: Every standalone utility function is suspect - functions that should be methods on a type that owns the behavior
3. **String Typing Violations**: Every string representing finite values should be an enum (exceptions: format validation, arbitrary text processing)
4. **State Machine Failures**: State tracking with primitives instead of types - boolean flags that should be part of state enums
5. **Builder Pattern Opportunities**: Complex construction that needs structure
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

<DuplicationPrevention>
**MANDATORY DUPLICATION DETECTION AND ELIMINATION**:

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
