## MAIN WORKFLOW

<ExecutionSteps>
    **CRITICAL: You MUST use the Task tool for reviews. Do NOT review code directly.**

    **ADOPT YOUR PERSONA:** Execute <ReviewPersona/> to establish your review expertise and mindset

    **EXECUTE THESE STEPS IN ORDER - ALL 5 STEPS ARE MANDATORY:**

    **STEP 1:** Execute the <InitialReview/> - MUST use Task tool
    **STEP 2:** Summarize the subagent's findings using <InitialReviewSummary/>
    **STEP 3:** Execute <FindingPrioritization/> - select top findings if needed
    **STEP 4:** Execute the <ReviewFollowup/> - MUST use Task tool for each finding
    **STEP 5:** Execute the <UserReview/>

    **DO NOT STOP until all 5 steps are complete.** Each step must flow directly into the next.
</ExecutionSteps>

<PlanDocument>
  The ${PLAN_DOCUMENT} is either the plan we've been working on or is in
  $ARGUMENTS if it is provided.
</PlanDocument>

## STEP 1: INITIAL REVIEW

<InitialReview>
    **1. Execute <DetermineReviewTarget/> from the specific review command**

    **2. Show user what you're reviewing:**
    <InitialReviewOutput/>

    **3. MANDATORY: Launch Task tool (DO NOT skip this):**
    - description: "review ${PLAN_DOCUMENT}" OR "review ${REVIEW_TARGET}"
    - subagent_type: "general-purpose"
    - prompt: Everything in <InitialReviewPrompt> below with placeholders replaced

    **4. After subagent completes: IMMEDIATELY PROCEED TO STEP 2**
    **DO NOT STOP HERE** - The review workflow requires all 5 steps to complete.
    Parse the subagent's JSON response and continue to <InitialReviewSummary/>
</InitialReview>

<InitialReviewPrompt>
    **ADOPT YOUR REVIEW PERSONA**: <ReviewPersona/>

    **Target:** ${REVIEW_TARGET}
    **CRITICAL CONTEXT**: ${REVIEW_CONTEXT}
    **WARNING**: This is a plan for FUTURE changes. Do NOT report issues about planned features not existing in current code - they don't exist because they haven't been built yet!
    **Review Constraints**: Follow these analysis principles:
    <InitialReviewConstraints/>

    **NOTE**: If <InitialReviewConstraints/> is not defined in the review command file, it will automatically fall back to <ReviewConstraints/>. Commands like design_review.md use phase-specific constraints, while code_review.md and command_review.md use a single constraint set for both phases.

    **NAMED FINDING DETECTION** (if applicable): <NamedFindingDetection/>

    Review ${REVIEW_TARGET} using the categories defined above and provide structured findings.
    It is important that you think very hard about this review task.

    **CRITICAL ID GENERATION REQUIREMENT**: <IDGenerationRules/>

    **CRITICAL CODE IDENTIFICATION REQUIREMENT**:
    **For DESIGN/PLAN REVIEWS**: <PlanCodeIdentification/>

    **For CODE REVIEWS**:
    1. Focus on implementation quality, safety, and design issues in the code

    **CODE CONTEXT REQUIREMENT**: <CodeExtractionRequirements/>

    **CRITICAL**: Format your response message using the exact JSON structure specified in <InitialReviewJson/>. Include the JSON directly in your message response text - do not create any files.
</InitialReviewPrompt>

<InitialReviewJson>
    <JsonFormatInstructions/>
Format your response message with EXACTLY this JSON structure:
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

GOOD Example - IMPLEMENTATION-GAP finding:
```json
{
  "id": "IMPLEMENTATION-GAP-1",
  "title": "Query batching use case lacks implementation",
  "location": {
    "plan_reference": "Section: Use Cases - Efficient Query Batching",
    "code_file": "MISSING - No implementation section exists",
    "line_start": 0,
    "function": "N/A"
  },
  "issue": "Plan describes 'support for batching multiple queries in a single request for efficiency' as a use case, but no implementation section covers how to add batching support",
  "current_code": "N/A - Implementation missing from plan",
  "suggested_code": "Add implementation section:\n1. Add BatchedQuery struct to types.rs\n2. Modify execute_query() to detect and split batches\n3. Add batch_results() aggregation function\n4. Update API to accept array of queries",
  "priority": "High",
  "impact": "Critical feature will be completely missed during implementation"
}
```
</CodeIdentificationExamples>

<InitialReviewSummary>

Provide a high-level summary of the subagent's findings:

# Review Summary
## Total findings: ${number}
- Categories found: ${list_categories_with_counts}
[If any named findings exist: - Named findings (skip investigation): ${count}]

## Key themes:
[2-3 bullet points about main issues identified]

[If named findings exist: **Note**: ${count} named finding(s) will skip investigation as they are self-evident violations]

**Next**: Proceeding to Step 3 (Finding Prioritization) and then Step 4 (Investigation)

</InitialReviewSummary>

<FindingPrioritization>
**MANDATORY: Display this structured output to force correct calculation:**

```
Step 3: Finding Prioritization

Total Findings: ${TOTAL_FINDINGS}
Max Followup Reviews: ${MAX_FOLLOWUP_REVIEWS}
Findings to Review: min(${TOTAL_FINDINGS}, ${MAX_FOLLOWUP_REVIEWS}) = ${SELECTED_COUNT}
```

**If SELECTED_COUNT < TOTAL_FINDINGS (prioritization needed):**

Display the prioritization:
```
Selected for Review (${SELECTED_COUNT} findings):
${list_each_selected_finding_id_and_title}

Deferred (${TOTAL_FINDINGS - SELECTED_COUNT} findings):
${list_each_deferred_finding_id_and_title}
```

**Selection criteria** (execute internally, don't output):
1. Include all named findings first (up to MAX_FOLLOWUP_REVIEWS)
2. Score remaining by: Priority (HIGH=3, MEDIUM=2, LOW=1) + Category (TYPE-SYSTEM=3, DESIGN/QUALITY=2, other=1) + Impact (High=3, Medium=2, Low=1)
3. Select top ${MAX_FOLLOWUP_REVIEWS} by score
4. Store deferred findings for <FinalSummary/>

**If SELECTED_COUNT = TOTAL_FINDINGS (no prioritization needed):**

No output needed - silently proceed to Step 4

**Create FILTERED_FINDINGS list:**
Extract exactly ${SELECTED_COUNT} findings and proceed immediately to <ReviewFollowup/>
</FindingPrioritization>

## STEP 4: INVESTIGATION

<ReviewFollowup>
    **NAMED FINDING FILTERING**:
    Before launching investigations, separate named findings from regular findings:
    1. Identify findings with "named_finding" field - these skip investigation
    2. Named findings already have a verdict (check calling command's <NamedFindings/>)
    3. Only investigate findings WITHOUT "named_finding" field
    4. If all findings are named findings: Skip to Step 5 (User Review)

    **CRITICAL PARALLEL EXECUTION REQUIREMENT**:
    You MUST send ALL ${N} Task tool calls for NON-NAMED findings in a SINGLE message with MULTIPLE antml:invoke blocks.

    **VIOLATION EXAMPLES**:
    - ❌ Sending one Task, waiting for completion, then sending another
    - ❌ Using multiple messages to send Tasks
    - ❌ Starting with "I'll investigate finding 1" instead of "I'll investigate all ${N} findings"
    - ❌ Investigating findings that have "named_finding" field

    **MANDATORY: Display this structured output before launching investigations:**

    ```
    Step 4: Investigation

    Total Findings: ${TOTAL_FINDINGS_FROM_STEP3}
    Findings to Investigate: ${INVESTIGATION_COUNT}
    ```

    Where:
    - TOTAL_FINDINGS_FROM_STEP3 = The count from Step 3 (should be ≤ MAX_FOLLOWUP_REVIEWS)
    - INVESTIGATION_COUNT = TOTAL_FINDINGS_FROM_STEP3 minus any named findings

    **THEN EXECUTE INVESTIGATION**:
    Send ONE message with ALL Task calls:
    - Single antml:function_calls block
    - ${INVESTIGATION_COUNT} antml:invoke elements (ALL in the same block)
    - Each with: description="Investigate FINDING-X: Title (X of INVESTIGATION_COUNT)"

    **ENFORCEMENT**: All ${INVESTIGATION_COUNT} Tasks MUST launch together in one message.

    **CRITICAL**: Investigations update verdicts only. DO NOT execute keyword actions or use Edit/Write tools. Present updated findings to user and wait for keyword selection.

    **5. After ALL investigation subagents complete: IMMEDIATELY PROCEED TO STEP 5**
    **DO NOT STOP HERE** - Parse all investigation JSON responses and continue to <UserReview/>
</ReviewFollowup>

<ReviewFollowupPrompt>
    **Investigation Task for Finding [CATEGORY-ID]**

    **ADOPT YOUR REVIEW PERSONA**: <ReviewPersona/>

    Review the following finding from the initial review:

    **Original Finding (JSON):**
    ```json
    [Insert the complete finding object from the InitialReviewJson findings array]
    ```

    Analyze this finding and provide an investigation verdict. It is important that you think very hard about this review task.

    **Review Constraints**: Follow these analysis principles:
    <InvestigationConstraints/>

    **NOTE**: If <InvestigationConstraints/> is not defined in the review command file, it will automatically fall back to <ReviewConstraints/>. Commands like design_review.md use phase-specific constraints (with different verification gates for investigation), while code_review.md and command_review.md use the same constraints for both phases.

    **Your Investigation Tasks:**
    1. Verify if this is a real issue or false positive
    2. Consider maintenance and long-term implications
    3. Provide a verdict: ${EXPECTED_VERDICTS}
    4. Include detailed reasoning for your verdict in simple, easy-to-understand terms
       - Avoid technical jargon where possible
       - Explain the "why" in plain language
       - Focus on practical impact rather than theoretical concepts
    6. **CRITICAL**: For any verdict that recommends action (CONFIRMED, FIX RECOMMENDED, MODIFIED, etc.),
       you MUST include concrete suggested_code showing the improved implementation
       - For MODIFIED verdicts: Show your alternative approach as actual code, not just a description
    7. **CRITICAL**: If the original finding has insufficient code context,
       you MUST read the file and provide the full context following <CodeExtractionRequirements/>
    8. **CRITICAL FOR PLAN REVIEWS**: If the original finding references a plan document,
       follow <PlanCodeIdentification/> requirements
    9. **CRITICAL FOR REJECTED VERDICTS**: You MUST clearly explain:
       - What the current plan/code does
       - What the finding incorrectly suggested
       - Why the current approach is actually correct
       - Use the format "The finding is incorrect because..." to be explicit
    10. **CRITICAL FOR ALL VERDICTS**: Structure your reasoning to clearly separate:
        - What problem the finding identified
        - What the current code/plan actually does
        - What change is being proposed
        - Why you agree/disagree/modify the proposal
        - Use plain language that a developer can quickly understand

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

    **CRITICAL**: Format your response message using the EXACT JSON structure specified in <ReviewFollowupJson/>. Include the JSON directly in your message response text - do not create any files.
</ReviewFollowupPrompt>

<ReviewFollowupJson>
<JsonFormatInstructions/>
Format your response message with EXACTLY this JSON structure (extending the <BaseReviewJson/> structure):
```json
{
  // All fields from <BaseReviewJson/> (keep original values unless updating)
  ...BaseReviewJson,

  // Additional fields for investigation followup:
  "verdict": "[CONFIRMED | MODIFIED | REJECTED | etc.]",
  "reasoning": "[Clear explanation of verdict decision in simple, easy-to-understand terms - avoid jargon, focus on practical impact]",
  "alternative_approach": "[Optional - describe alternatives if applicable]"
}
```

Additional Requirements for Followup:
- Include ALL fields from the original finding (as defined in <BaseReviewJson/>)
- verdict must be one of: ${EXPECTED_VERDICTS}
- alternative_approach is OPTIONAL - omit if not applicable
- For verdicts recommending action: suggested_code is REQUIRED with concrete implementation
- **CRITICAL FOR MODIFIED VERDICT**: Must include suggested_code showing the alternative approach in concrete code
- For verdicts NOT recommending action: omit suggested_code field
- suggested_code must show the actual fixed/improved code, not instructions about what to change
- current_code field MUST be expanded if original had insufficient context (minimum 5-10 lines)
</ReviewFollowupJson>

## STEP 5: USER INTERACTION

<UserOutput>
Present the investigation findings to the user using this format
(derived from the ReviewFollowupJson data).
**Note**: Use appropriate language identifier in code blocks (rust, python, javascript, etc.):

# **${id}**: ${title} (${current_number} of ${total_findings})
**Issue**: ${issue}
[if location.function exists: **Function**: ${location.function}]
[if location.plan_reference exists: **Plan Reference**: ${location.plan_reference}]

### What the finding is saying:
[Clear, concise summary of what problem or improvement the finding identified]

## Current Code
**Location**: ${location.code_file}:${location.line_start}
```rust
${current_code}
```

### Commentary on current code:
[Brief explanation of what the current code does and why it might be problematic according to the finding]

### What the finding is proposing:
[Clear, concise summary of the suggested change and how it will be used]

## Suggested Code Change
```rust
${suggested_code}
```

## Reviewer's Assessment

[FOR CONFIRMED/FIX RECOMMENDED verdicts:]
### Why this change is needed:
[Explain why the finding is correct and the change should be made]

### Expected impact:
[What will improve after making this change]

[FOR MODIFIED/FIX MODIFIED verdicts:]
### Original suggestion issue:
[What was problematic about the original suggestion]

### Better approach:
[Why the modified version is superior]

### Expected impact:
[What will improve with the modified approach]

[FOR REJECTED/FIX NOT RECOMMENDED verdicts:]
### What the current approach does:
[Clear explanation of the existing code/plan's approach]

### Why the finding is incorrect:
[Specific reasons why the suggested change is unnecessary or wrong]

### Recommendation: Keep as-is
The current approach is correct. No changes needed.

## **Verdict**: ${verdict}
</UserOutput>

<NamedFindingOutput>
    **For Named Findings (findings with "named_finding" field)**:

    1. **Lookup Template**: Find the output template in calling command's <NamedFindingOutputTemplates/>
       - Match ${finding.named_finding} to the template name
       - Example: "line_number_violation" → <LineNumberViolationOutput/>

    2. **Use Specialized Template**: Present using the named finding template
       - Replace placeholders with finding data
       - Template already includes verdict (no investigation needed)
       - Skip investigation Task for this finding

    3. **Fallback**: If template not found, use standard <UserOutput/> format
       - Use the auto-verdict from calling command's <NamedFindings/>
       - Add note: "Named finding - investigation skipped"

    4. **Keywords**: Still present appropriate keywords based on the auto-verdict
       - Named findings typically have CONFIRMED verdict
       - User can still choose to skip, investigate, etc.
</NamedFindingOutput>

<UserReview>
    **Present Findings to User for Interactive Review**

    **FIRST**: Create a TodoWrite list to track the review process:
    - Use TodoWrite tool to create todos for each finding
    - Simply describe the todo content and status - don't encode the JSON format
    - Example: "Create todos for: 'Review ${id}: ${title}' with status pending for each finding"
    - Mark each as "in_progress" when presenting it
    - Mark as "completed" after user responds with a keyword

    **TRACK DECISIONS**: Maintain a list of all decisions made and what changes they caused.

    For each finding (in order, starting with TYPE-SYSTEM):

    1. Update TodoWrite to mark current finding as "in_progress"
    2. **MANDATORY**: Apply <PriorDecisionReconciliation/> before presenting any finding
    3. **Check for Named Finding**:
       - If finding has "named_finding" field:
         a. Note: "Named finding detected: ${finding.named_finding} - using specialized output"
         b. Use template from <NamedFindingOutputTemplates/> in calling command
         c. Set verdict from calling command's <NamedFindings/> registry
         d. Skip to step 4 (no investigation results to use)
       - Otherwise: Present using format from <UserOutput/> (from investigation results)
    4. Include the (n of m) counter in the title
       - Add any relevant notes about how prior decisions affect this finding
    5. Display available keywords using <FormatKeywords verdict="${VERDICT}"/> based on the verdict
       - **CRITICAL**: For named findings, use auto-verdict from <NamedFindings/>
       - **CRITICAL**: For investigated findings, use UPDATED verdict from investigation result
    6. STOP and wait for user's response
    7. **CRITICAL USER RESPONSE HANDLING**:
       - **IF USER PROVIDES A KEYWORD**: Execute the action from your command file's <KeywordExecution/> section
       - **IF USER PROVIDES ANY OTHER RESPONSE** (discussion, alternative proposal, question, clarification):
         a. Engage with the user's input appropriately
         b. If discussion leads to agreement/resolution, summarize the agreed approach
         c. **CRITICAL**: If you take any action that modifies the plan (via Edit/Write tools):
            - Mark the current todo as "completed"
            - Ask "Edit complete. Type 'continue' to proceed to the next finding. (${current_number} of ${total_findings})"
            - Wait for user confirmation before proceeding
            - Skip steps d-f below and continue from step 8
         d. **MANDATORY**: Present keywords using <FormatKeywords verdict="${CURRENT_VERDICT}"/>
         e. **MANDATORY**: State "Please select one of the keywords above to proceed."
         f. **DO NOT CONTINUE** to next finding until user provides a keyword
         g. If user continues discussion instead of selecting keyword, repeat steps a-f
       - **SPECIAL CASE FOR "investigate" KEYWORD**: After investigation completes,
         a. Parse the investigation JSON result to extract updated verdict and reasoning
         b. Update the finding object with new verdict, reasoning, etc.
         c. Present the finding again using the same <UserOutput/> format but with updated information
         d. Present keywords appropriate for the NEW verdict (not the original verdict)
         e. Wait for user's new keyword response before proceeding
         f. Do NOT mark the todo as completed until user provides a keyword for the updated finding
    8. After keyword execution:
       - **FOR ALL EDIT ACTIONS** (agree, skip, skip with prejudice, accept as built, redundant):
         a. After executing the Edit tool to modify the plan
         b. STOP and ask:
            - If not the last finding: "Edit complete for ${current_finding_id}. Type 'continue' to proceed to ${next_finding_id} (${next_number} of ${total_findings})"
            - If this is the last finding: "Edit complete for ${current_finding_id}. Type 'continue' to complete the review - this was the final finding"
         c. Wait for user confirmation before proceeding
    9. Update TodoWrite to mark current finding as "completed" ONLY after user confirms continuation
    10. **USER CONTROL REQUIREMENTS**:
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
    ## ${finding.id}: ${finding.title} - **Verdict**: ${finding.verdict} ${additional_status_suffix}
    - **Status**: ${ACTION_STATUS}
    - **Location**: ${finding.location.plan_reference}
    - **Issue**: ${finding.issue}
    - **Reasoning**: ${finding.reasoning}
    ${action_specific_sections}
    ```

    Where:
    - ${additional_status_suffix}: Optional suffix like "✅", "- REDUNDANT", "- DEVIATION ACCEPTED"
    - ${ACTION_STATUS}: "SKIPPED", "APPROVED - To be implemented", "PERMANENTLY REJECTED", etc.
    - ${action_specific_sections}: Additional sections based on the specific action
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
- ${additional_status_suffix}: (none)
- ${ACTION_STATUS}: "SKIPPED"
- ${action_specific_sections}: "- **Decision**: User elected to skip this recommendation"
</SkipTemplate>

<SkipWithPrejudiceTemplate>
Use <ReviewFindingBaseTemplate/> with:
- ${additional_status_suffix}: (none, but prefix with "⚠️ PREJUDICE WARNING - " before finding.id)
- ${ACTION_STATUS}: "PERMANENTLY REJECTED"
- ${action_specific_sections}: "- **Critical Note**: DO NOT SUGGEST THIS AGAIN - Permanently rejected by user"
</SkipWithPrejudiceTemplate>

<RedundantTemplate>
Use <ReviewFindingBaseTemplate/> with:
- ${additional_status_suffix}: "- REDUNDANT"
- ${ACTION_STATUS}: "REDUNDANT - Already addressed in plan"
- ${action_specific_sections}:
  "- **Existing Implementation**: ${quote_relevant_section}
  - **Plan Section**: ${section_title}
  - **Critical Note**: This functionality/design already exists in the plan - future reviewers should check for existing coverage before suggesting"
</RedundantTemplate>


<AcceptAsBuiltTemplate>
Use <ReviewFindingBaseTemplate/> with:
- ${additional_status_suffix}: "- DEVIATION ACCEPTED"
- ${ACTION_STATUS}: "ACCEPTED AS BUILT"
- ${action_specific_sections}:
  "- **Plan Specification**: ${plan_specification}
  - **Actual Implementation**: ${finding.current_code}
  - **Decision**: Implementation deviation accepted and documented"
</AcceptAsBuiltTemplate>

<FormatKeywords>
**CRITICAL**: This section produces user-facing output. Always ensure output starts at column 0 for proper markdown formatting.

Based on the verdict parameter, format the appropriate keywords for the current review type:

**For Design Review:**
- CONFIRMED/MODIFIED verdicts: <DesignConfirmedKeywords/>
- REJECTED verdicts: <DesignRejectedKeywords/>

**For Code Review:**
- FIX RECOMMENDED/FIX MODIFIED verdicts: <CodeFixKeywords/>
- FIX NOT RECOMMENDED verdicts: <CodeNoFixKeywords/>

**For Command Review:**
- ENHANCE/REVISE verdicts: <CommandEnhanceKeywords/>
- SOLID verdicts: <CommandSolidKeywords/>
</FormatKeywords>

<DesignConfirmedKeywords>
## Available Actions
- **agree** - Update plan document with the suggested design improvement
- **skip** - Add to "Design Review Skip Notes" section and continue
- **skip silently** - Reject without updating the plan document
- **skip with prejudice** - Permanently reject with ⚠️ PREJUDICE WARNING
- **redundant** - Mark as redundant - the suggestion already exists in the plan
- **investigate** - Launch deeper investigation of the design issue
</DesignConfirmedKeywords>

<DesignRejectedKeywords>
## Available Actions
- **override** - Override the rejection - treat as CONFIRMED and implement the suggestion
- **agree** - Accept that the finding was incorrect - plan stays unchanged
- **agree silently** - Accept the rejection without updating the plan document
- **skip with prejudice** - Permanently reject with ⚠️ PREJUDICE WARNING
- **investigate** - Challenge the rejection and investigate further
</DesignRejectedKeywords>

<CodeFixKeywords>
## Available Actions
- **fix** - Apply the suggested code change
- **skip** - Skip this fix and continue
- **investigate** - Launch deeper investigation of the code issue
</CodeFixKeywords>

<CodeNoFixKeywords>
## Available Actions
- **accept** - Accept that the code is correct as-is
- **override** - Apply the fix despite the recommendation
- **investigate** - Launch investigation to reconsider
</CodeNoFixKeywords>

<CommandEnhanceKeywords>
## Available Actions
- **improve** - Apply the suggested improvements to the command
- **skip** - Skip this improvement and continue
- **investigate** - Launch deeper investigation
</CommandEnhanceKeywords>

<CommandSolidKeywords>
## Available Actions
- **accept** - Accept that the command is well-structured
- **override** - Apply the improvement despite the recommendation
- **investigate** - Launch investigation to reconsider
</CommandSolidKeywords>

<FinalSummary>
    Review Complete!
    - Total findings reviewed: ${count}
    - Actions taken: ${actions_taken}
    - Categories addressed: [list with counts]

    [If findings were deferred:]

    **Deferred Findings** (not reviewed in this session):
    The following ${count} findings were prioritized lower and not reviewed. You can run the review again to address these:
    ${list_each_deferred_finding_with_id_title_category_priority}

    To review deferred findings, run this command again and I will pick up where we left off.
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

## SHARED CONSTRAINTS
Constraints used by multiple review types

<TypeSystemPrinciples>
Follow these type system design principles as highest priority:
1. **Conditional Audit**: Look for problematic conditionals that could be improved with better type design
   - **PROBLEMATIC**: String comparisons (e.g., `if kind == "enum"` or `if type_name.contains("Vec")`)
   - **PROBLEMATIC**: Boolean combinations that represent states (e.g., `if is_valid && !is_empty && has_data`)
   - **PROBLEMATIC**: Numeric comparisons for state (e.g., `if status == 1` or `if phase > 3`)
   - **CORRECT**: Enum pattern matching with `match` or `matches!` macro - this is proper type-driven design
   - **CORRECT**: Simple boolean checks for actual binary states
   - **DO NOT REPORT**: Proper enum pattern matching as a violation - `matches!(value, Enum::Variant)` is idiomatic Rust
2. **Function vs Method Audit**: Every standalone utility function is suspect - functions that should be methods on a type that owns the behavior
3. **String Typing Violations**: Every string representing finite values should be an enum (exceptions: format validation, arbitrary text processing, actual text content)
   - **PROBLEMATIC**: Using strings for type names, kinds, or categories
   - **CORRECT**: Using strings for user messages, file paths, or actual text data
4. **State Machine Failures**: State tracking with primitives instead of types - boolean flags that should be part of state enums
   - **PROBLEMATIC**: Multiple booleans that together represent a state
   - **CORRECT**: Single boolean for truly binary conditions
5. **Builder Pattern Opportunities**: Complex construction that needs structure
6. **No Magic Values**: Never allow magic literals - use named constants or enums that can serialize - ideally with conversion traits for ease of use
</TypeSystemPrinciples>

## SHARED DEFINITIONS
Reusable components and definitions used throughout the review workflow

<BaseReviewJson>
Base JSON structure for review findings:
```json
{
  "id": "${CATEGORY}-${NUMBER}",
  "category": "${CATEGORY}",
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
  "impact": "[Why this matters]",
  "named_finding": "[Optional - for self-evident violations like 'line_number_violation']"
}
```

Base Requirements:
- ${CATEGORY} must be from <ReviewCategories/> (e.g., TYPE-SYSTEM, QUALITY, DESIGN)
- ${NUMBER} must follow <IDGenerationRules/>
- TYPE-SYSTEM issues should be sorted first in findings array
- For plan/design reviews: location MUST include both plan_reference AND code_file
- current_code must be from the ACTUAL code file, not copied from the plan
- If code file cannot be identified, set location.code_file to "UNKNOWN - NEEDS INVESTIGATION"
- current_code must follow <CodeExtractionRequirements/>
- current_code must be PURE CODE ONLY - no markdown headers, instructions, or prose
- named_finding is OPTIONAL - only include for violations defined in calling command's <NamedFindings/>
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
**CRITICAL**: Format your response message as JSON text for the main agent to parse. Do NOT create, write, or save any files. Do NOT use the Write, Edit, or any file creation tools.

Include the JSON structure directly in your response message text. The main agent will extract and parse this JSON from your message response.

**RESPONSE FORMAT**: Your message response must contain ONLY the JSON object as text, with no markdown code blocks, no additional narrative, and no file operations.
</JsonFormatInstructions>

