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
      "location": "[File path and line numbers or section reference]",
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
- [NUMBER] is sequential within category (1, 2, 3...)
- TYPE-SYSTEM issues always have priority: "High"
- Sort findings array with TYPE-SYSTEM issues first
- current_code MUST contain enough context (5-10+ lines, complete functions when relevant)
- Return ONLY the JSON object, no additional text
</InitialReviewJson>

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

    Launch parallel investigations:
    **CRITICAL**: You MUST launch ALL investigations in a SINGLE response using multiple Task tool calls.
    
    1. **SINGLE MESSAGE WITH MULTIPLE TASK CALLS**: For EACH finding from the initial review, create a separate Task tool invocation with:
       - description: "Investigate [CATEGORY-ID]: [Brief title of finding]"
       - subagent_type: "general-purpose"
       - prompt: Use the template from <ReviewFollowupPrompt/> with the specific finding details
    
    2. **EXAMPLE**: If you have 3 findings, your response should contain exactly 3 Task tool calls, all in one message
    
    3. Wait for ALL investigation subagents to complete (they will return ReviewFollowupJson)
    4. Parse the JSON results and merge with original findings
    5. Prepare to present each investigated finding to the user in the next step

    **CRITICAL**: Do NOT present findings to the user yet - just complete the investigations and compile results.
    **CRITICAL**: Do NOT wait between Task calls - send them all at once for true parallel execution.
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
    6. Be sure to include suggested code changes when recommending action
    7. **CRITICAL**: If the original finding has insufficient code context (less than 5 lines), 
       you MUST read the file and provide the full context in your response

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
  "location": "[File path and line numbers from original]",
  "issue": "[Specific problem description from original]",
  "current_code": "[Code snippet from original]",
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
- For verdicts recommending action: suggested_code is REQUIRED
- For verdicts NOT recommending action: omit suggested_code field
- current_code field MUST be expanded if original had insufficient context (minimum 5-10 lines)
- Return ONLY the JSON object, no additional text
</ReviewFollowupJson>

## STEP 3: USER INTERACTION

<UserOutput>
Present the investigation findings to the user using this format
(derived from the ReviewFollowupJson data).
**Note**: Use appropriate language identifier in code blocks (rust, python, javascript, etc.):

# **[id]**: [title] ([current_number] of [total_findings])
**Issue**: [issue]
**Location**: [location]
**Impact**: [impact]
**Priority**: [priority]
**Complexity**: [complexity]
**Risk**: [risk]

## Current Code
```rust
[current_code]
```

## Suggested Code Change
```rust
[suggested_code - only include if verdict recommends action]
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

    For each investigated finding (in order, starting with TYPE-SYSTEM):

    1. Update TodoWrite to mark current finding as "in_progress"
    2. Present the finding using the format from <UserOutput/> (from the investigation results)
       - Include the (n of m) counter in the title
    3. Display available keywords using <KeywordPresentation/> format based on the verdict:
       - Identify the verdict from investigation (e.g., CONFIRMED, FIX RECOMMENDED)
       - Look in your command file's <ReviewKeywords/> section for that specific verdict
       - Present ONLY the keywords listed for that verdict
       - Use the exact descriptions provided in <ReviewKeywords/>
    4. STOP and wait for user's keyword response
    5. Execute the action from your command file's <KeywordExecution/> section
    6. Update TodoWrite to mark current finding as "completed"
    7. Continue to next finding

    After all findings are reviewed, provide a summary using <FinalSummary/>
</UserReview>

<PlanUpdateFormat>
    **CRITICAL**: When updating plan documents with review findings, you MUST include:
    - The finding ID and title
    - The category
    - The location where the issue was found
    - The actual issue description
    - The verdict from the investigation
    - The reasoning from the investigation verdict
    - Any relevant code or suggested changes
    
    Use the appropriate template below based on the keyword action.
</PlanUpdateFormat>

<SkipTemplate>
### [id]: [title]
- **Status**: SKIPPED
- **Category**: [category]
- **Location**: [location from finding]
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
- **Location**: [location from finding]
- **Issue**: [issue from finding]
- **Verdict**: [verdict from investigation]
- **Reasoning**: [reasoning from investigation]
- **Critical Note**: DO NOT SUGGEST THIS AGAIN - Permanently rejected by user
</SkipWithPrejudiceTemplate>

<AgreeTemplate>
## [id]: [title] ✅
- **Category**: [category]
- **Status**: APPROVED - To be implemented
- **Location**: [location from finding]
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
- **Location**: [location in code]
- **Plan Specification**: [What the plan originally specified]
- **Actual Implementation**: [current_code or description]
- **Verdict**: [verdict from investigation]
- **Reasoning**: [reasoning from investigation]
- **Decision**: Implementation deviation accepted and documented
</AcceptAsBuiltTemplate>

<KeywordPresentation>
    ## Available Actions
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
