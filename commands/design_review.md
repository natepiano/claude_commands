# Actionable Design Review Command

## Arguments
If `$ARGUMENTS` is provided, it should be the name of a `plan*.md` file in the project root that will be the target of the design review. For example:
- `plan_enum_generation.md`
- `plan_mutation_system.md`
- `plan_api_redesign.md`

If no arguments are provided, the design review will target the plan markdown document currently being worked on.

## Overview
Use the Task tool with a general-purpose agent to conduct a comprehensive design review that produces **actionable recommendations for discussion**. Be sure to instruct the agent to think hard about the items in the <ReviewScope/>. The primary goal is to edit the plan markdown file being reviewed with approved suggestions and track skipped items.

**CRITICAL**: After receiving the subagent's review, follow the 4-step workflow from @shared/post_review_workflow.txt:
1. **RECEIVE SUBAGENT'S REVIEW**: Get the structured findings with code examples
2. **PRESENT INITIAL SUMMARY**: Show summary statistics and overview table using `<CorePresentationFlow>` 
3. **EXECUTE PARALLEL INVESTIGATION**: Auto-investigate all findings with verdicts
4. **BEGIN KEYWORD-DRIVEN REVIEW**: Present each recommendation one at a time with keywords

## Review Scope
- Analyze the current implementation plan for completeness and feasibility
- Identify gaps, missing components, and implementation issues
- Focus on breaking changes, over-engineering, and simplification opportunities
- Provide concrete, specific recommendations that can be turned into todo items
- **CRITICAL**: Do NOT include any timing, schedules, timelines, or time estimates in recommendations

## Required Output Format

Use the standard output format from `<StandardOutputFormat>` in @shared/keyword_review_pattern.txt with these parameters:

**Summary Parameters:**
- **[REVIEW_SUMMARY_TITLE]**: "Summary"
- **[REVIEW_TARGET_DESCRIPTION]**: "findings"
- **[POSITIVE_METRIC_LABEL]**: "Positive findings"
- **[ACTION_METRIC_LABEL]**: "Suggested improvements"
- **[POSITIVE_FINDINGS_SECTION_TITLE]**: "Positive Findings (Good Design Practices Observed)"

**Findings Parameters:**
- **[FINDINGS_SECTION_TITLE]**: "Actionable Recommendations"
- **[PROBLEMS_DESCRIPTION]**: "design gaps or improvements"
- **[ACTION_TYPE]**: "plan improvement"
- **[FINDINGS_TYPE]**: "recommendations"

**Category Parameters:**
- **[CATEGORY-TYPE]**: TYPE-SYSTEM-*, DESIGN-*, IMPLEMENTATION-*, SIMPLIFICATION-*
- **[PRIMARY_FIELD_LABEL]**: Varies by category:
  - TYPE-SYSTEM: "Type System Gap"
  - DESIGN: "Plan Issue" 
  - IMPLEMENTATION: "Plan Gap"
  - SIMPLIFICATION: "Over-Engineering in Plan"
- **[LOCATION_FIELD]**: "Plan Section"
- **[CURRENT_STATE_FIELD]**: "Current Plan Text" or "Plan's Current Approach"  
- **[PROPOSED_CHANGE_FIELD]**: "Proposed Plan Enhancement" or "Plan Improvement Proposal"
- **[IMPACT_FIELD_LABEL]**: "Rationale" or "Benefits"

**Design Review Specific Requirements:**
- Include exact section names where plan edits should be made
- Provide markdown quotes for plan text and ```rust code blocks for code examples
- Focus recommendations on improving THE PLAN DOCUMENT, not implementing code
- All TYPE-SYSTEM recommendations must be HIGH priority

## Analysis Requirements

**CRITICAL**: Read and follow @shared/keyword_review_pattern.txt section `<TypeSystemDesignPrinciples>` for comprehensive analysis guidance.

### Design Review Specific Analysis
Beyond the shared principles, also analyze:
1. **Gap Analysis**: Missing components, unclear specifications
2. **Breaking Changes**: API changes, signature modifications, structural changes
3. **Over-engineering**: Unnecessary complexity that can be simplified
4. **Implementation Order**: Dependencies and sequencing issues (NO time estimates)
5. **Code Examples**: Only when essential for clarity (prefer brief descriptions)
6. **NO TIMING**: Never include schedules, timelines, deadlines, or time estimates
7. **NO BACKWARDS COMPATIBILITY SUGGESTIONS**: Never suggest adding backwards compatibility - our designs are always complete changes. If the plan needs backwards compatibility, it will specify it explicitly

## Prompt Template

Use the shared subagent prompt template from @shared/subagent_prompt_template.txt with these parameters:

- **[REVIEW_TARGET]**: "[REVIEW TARGET: plan markdown document (default), specific files, or focus area] to evaluate THE PLAN'S DESIGN QUALITY"
- **[OUTPUT_DESCRIPTION]**: "recommendations for IMPROVING THE PLAN ITSELF"
- **[REVIEW_CONTEXT]**: "You are reviewing a PLAN DOCUMENT to improve its design, NOT checking if it has been implemented."
- **[CONTEXT_DETAILS]**: 
  - The plan describes FUTURE work to be done
  - Your job is to identify gaps, over-engineering, or improvements IN THE PLAN
  - You should read current code to understand context, but you're improving THE PLAN, not the code
- **[TARGET_SELECTION_INSTRUCTIONS]**: 
  - If `$ARGUMENTS` is provided, review the specified `plan*.md` file in the project root
  - If no arguments are provided, review the plan markdown document currently being worked on
  - Include specific section names where edits should be made in the target document
- **[STEP_1_LABEL]**: "MANDATORY FIRST STEP - CHECK SKIP NOTES"
- **[STEP_1_INSTRUCTIONS]**: 
  ```
  <SkipNotesCheck>
  1. **IMMEDIATELY** look for a "Design Review Skip Notes" section in the document
  2. **READ EVERY SINGLE SKIPPED ITEM** - extract the core concept, not just the ID
  3. **UNDERSTAND REJECTION REASONS** - if something was rejected for being "already covered", don't suggest similar items
  4. **CHECK FOR SIMILAR CONCEPTS** - don't re-suggest the same idea with different wording
  5. **PAY SPECIAL ATTENTION** to items marked with "⚠️ PREJUDICE WARNING" - suggesting these again is a CRITICAL FAILURE
  6. **CROSS-REFERENCE YOUR IDEAS** - before making any recommendation, verify it's not a variation of something already rejected
  7. **WHEN IN DOUBT, DON'T SUGGEST** - if your idea is remotely similar to a skipped item, skip it
  8. **DO NOT PROCEED** until you have confirmed what has been previously skipped AND checked your recommendations against them
  </SkipNotesCheck>
  ```
- **[STEP_2_LABEL]**: "MANDATORY SECOND STEP - THOROUGH DOCUMENT READING"
- **[STEP_2_INSTRUCTIONS]**: 
  ```
  <DocumentComprehension>
  1. **READ THE ENTIRE PLAN** from beginning to end before making any recommendations
  2. **SEARCH FOR EXISTING SOLUTIONS** - before claiming something is missing, search the document for related content
  3. **QUOTE SPECIFIC SECTIONS** - when claiming gaps exist, quote the relevant plan sections and explain exactly what's missing
  4. **CROSS-REFERENCE SECTIONS** - many topics span multiple sections, check all related areas
  5. **VERIFICATION REQUIREMENT**: For every "missing" claim, you MUST either:
     - Quote the plan section that should contain it but doesn't, OR
     - Explain why the existing content is insufficient with specific examples
  </DocumentComprehension>
  ```
- **[STEP_3_LABEL]**: "MANDATORY THIRD STEP - UNDERSTAND THE REVIEW SCOPE"
- **[STEP_3_INSTRUCTIONS]**: 
  ```
  <ReviewScope>
  1. Carefully read and understand what you're reviewing (plan, files, or specific focus area)
  2. Note what is already present, implemented, or explicitly planned in the review scope
  3. DO NOT suggest things that are already part of what you're reviewing
  4. DO NOT suggest anything that appears in the Skip Notes section
  5. Focus on genuine gaps, improvements, and issues not already addressed
  </ReviewScope>
  ```
- **[STEP_4_LABEL]**: "CRITICAL FOURTH STEP - TYPE SYSTEM VIOLATIONS"
- **[STEP_4_INSTRUCTIONS]**: "Follow `<TypeSystemDesignPrinciples>` from the shared pattern file exactly. This includes all primary type system violations, error handling standards, and analysis priority order."
- **[FINAL_FILTER_LABEL]**: "MANDATORY FINAL FILTER - DUPLICATE PREVENTION"
- **[FINAL_FILTER_INSTRUCTIONS]**: 
  ```
  Before including ANY recommendation in your output:
  1. **SKIP NOTES VERIFICATION**: 
     - Check if the core concept appears in Skip Notes (even with different wording)
     - If ANYTHING similar was rejected, DO NOT suggest it
     - Pay special attention to rejection reasons like "already covered" or "already detailed"
  2. **CONTENT VERIFICATION**: 
     - Verify it's not already handled in what you're reviewing
     - Search the document for existing solutions to the same problem
  3. **FINAL CHECK**: 
     - Only suggest genuine improvements NOT already addressed
     - When in doubt about similarity to skipped items, SKIP IT
  
  **CRITICAL FAILURE MODES TO AVOID**:
  - ❌ Re-suggesting rejected concepts with different IDs or wording
  - ❌ Suggesting items that were rejected because "plan already covers this"
  - ❌ Ignoring Skip Notes rejection reasons
  - ❌ Suggesting something with ⚠️ PREJUDICE WARNING - this is a CRITICAL FAILURE
  - ❌ Suggesting backwards compatibility additions - designs are complete changes, not incremental
  
  **ENFORCEMENT**: Any recommendation that's a variation of a Skip Notes item will be immediately rejected and may result in prejudice warnings.
  ```
- **[SHARED_ANALYSIS_REFERENCE]**: "CRITICAL FOURTH STEP - TYPE SYSTEM VIOLATIONS"
- **[SHARED_REFERENCE_INSTRUCTIONS]**: "Follow `<TypeSystemDesignPrinciples>` from the shared pattern file exactly. This includes all primary type system violations, error handling standards, and analysis priority order."
- **[STANDARD_ANALYSIS_STEPS]**: 
  1. Identifying ACTUAL gaps and missing components (not things already present in the review scope)
  2. Finding breaking changes not explicitly called out
  3. Spotting over-engineering and unnecessary complexity
  4. Ensuring implementation order makes sense
  5. Creating concrete, discussable recommendations for improvements NOT already addressed
- **[REVIEW_SPECIFIC_REQUIREMENTS]**: 
  - **CRITICAL REMINDER**: Do NOT include any timing information in recommendations:
    - NO schedules, deadlines, or milestones
    - NO time estimates or durations
    - NO timeline or project phases with dates
    - Focus ONLY on technical design and implementation structure
  - **CRITICAL - IMPLEMENTATION PROPOSALS REQUIRED**: For each recommendation to improve THE PLAN, you MUST include concrete examples:
    - **Current Plan**: Show what the plan currently says about this area (quote from the plan document)
    - **Current Code**: Show the actual existing code that the plan is trying to improve (read files to get real code)  
    - **Proposed Plan Change**: Show how THE PLAN should be modified to address your recommendation
    - **Proposed Code Implementation**: Show concrete code examples of what the improved plan would produce
    - Use proper syntax highlighting with ```rust code blocks for code examples
    - Use markdown quotes for plan text
    - **ENFORCEMENT**: Any recommendation without Current Code and Proposed Code sections will be REJECTED
    - Your recommendations are for improving THE PLAN DOCUMENT, not for implementing code
- **[OUTPUT_FORMAT_REQUIREMENTS]**: "Return results in the structured format with TYPE-SYSTEM-*, DESIGN-*, IMPLEMENTATION-*, and SIMPLIFICATION-* categories."
- **[PRIORITY_INSTRUCTIONS]**: "TYPE-SYSTEM recommendations should come FIRST and be treated as highest priority."
- **[DETAILED_REQUIREMENTS]**: "For each recommendation, specify the exact location in the plan document where it should be added or edited (e.g., \"Add to Section 3.2 Implementation Details\" or \"Edit the API Design section\")."

## Post-Review Instructions - Auto-Investigation and Keyword Process

Follow the 4-step workflow from @shared/post_review_workflow.txt with these parameters:
- **[REVIEW_TYPE]**: "Design Review"
- **[FINDINGS_TYPE]**: "design review findings"
- **[ITEMS]**: "recommendations"
- **[ITEM]**: "recommendation"
- **[RESOLUTION_STATE]**: "already in Skip Notes or already addressed"
- **[DOMAIN_VALUES]**: "code elegance AND practical utility"
- **[INVESTIGATION_FOCUS_LIST]**:
  - Evaluate architectural merit and system design improvements
  - Assess type system usage to eliminate bugs and express intent
  - Consider API elegance and interface design quality
  - Analyze pattern consistency with existing codebase
  - Check for over-engineering vs solving real problems
  - Consider how this affects future evolution and extensions
- **[EXPECTED_VERDICTS]**: "CONFIRMED, MODIFIED, or REJECTED"
- **[UPDATE_TARGET]**: "plan document updates"
- **[CATEGORY_SET]**: "design review categories"

### Design Review Specific Keywords (Based on Verdict)
**For CONFIRMED/MODIFIED verdicts**: 
- `agree` - Add to plan document
- `skip` - Add to Skip Notes  
- `skip with prejudice` - Permanently reject with warning
- `investigate further` - Get targeted deeper analysis

**For REJECTED verdicts**:
- `agree` - Accept rejection (add to Skip Notes as "Investigated and Rejected")
- `override` - Add to plan despite rejection
- `skip with prejudice` - Permanently reject with warning
- `investigate further` - Get targeted deeper analysis

### Design Review Keyword Response Actions

Follow `<KeywordResponseWorkflow>` from @shared/keyword_review_pattern.txt with these parameters:
- **[ITEM]**: "recommendation"
- **[CUMULATIVE_REVIEW_SPECIFICS]**: "Update the suggestion to account for the cumulative effect of these changes - leverage newly added patterns, avoid redundant suggestions, and ensure consistency with what has already been approved."

**Review-Specific Actions**:

**When user responds "agree"**:
**[REVIEW_SPECIFIC_ACTION]**: **UPDATE PLAN DOCUMENT ONLY**: Add a new dedicated section for the agreed recommendation with:
- Full implementation details from the recommendation
- Specific code changes required (to be implemented later)
- File paths and line numbers to modify (for future reference)
- Step-by-step implementation instructions (for when implementation begins)
- Integration points with existing plan sections
**CROSS-REFERENCE**: Add references between the new plan section and related implementation sections

**When user responds "skip"**:
**[REVIEW_SPECIFIC_ACTION]**: **UPDATE SKIP NOTES**: Add/update "Design Review Skip Notes" section in the plan document using this EXACT format:
```markdown
## Design Review Skip Notes

### [RECOMMENDATION-ID]: [Title]
- **Status**: SKIPPED
- **Category**: [TYPE-SYSTEM/DESIGN/IMPLEMENTATION/SIMPLIFICATION]
- **Description**: [Brief description of what was skipped]
- **Reason**: User decision - not needed for current implementation
```

**When user responds "skip with prejudice"**:
**[REVIEW_SPECIFIC_ACTION]**: **UPDATE SKIP NOTES WITH PREJUDICE FLAG**: 
- If this recommendation was already skipped, APPEND prejudice warning to existing entry
- If new, add full entry to "Design Review Skip Notes" section

For existing skipped entry, append:
```markdown
- **⚠️ PREJUDICE WARNING**: PERMANENTLY REJECTED
- **Critical Note**: DO NOT SUGGEST THIS AGAIN - Reviewer repeatedly suggested this despite prior rejections
```

For new entry, use full format:
```markdown
## Design Review Skip Notes

### ⚠️ PREJUDICE WARNING - [RECOMMENDATION-ID]: [Title]
- **Status**: PERMANENTLY REJECTED
- **Category**: [TYPE-SYSTEM/DESIGN/IMPLEMENTATION/SIMPLIFICATION]
- **Description**: [Brief description of what was skipped]
- **Reason**: Reviewer repeatedly suggested this despite prior rejections
- **Critical Note**: DO NOT SUGGEST THIS AGAIN - This recommendation has been permanently rejected due to reviewer repetition
```

### Plan Update Template for "agree" Keyword

When user says "agree" for a recommendation, add this new section to the plan document (NOTE: No code changes are made during design review - only plan updates):

```markdown
## DESIGN REVIEW AGREEMENT: [RECOMMENDATION-ID] - [Title]

**Plan Status**: ✅ APPROVED - Ready for future implementation

### Problem Addressed
[Copy the "Issue" field from the recommendation]

### Solution Overview  
[Copy the "Proposed Type Design" or "Recommendation" field]

### Required Code Changes

#### Files to Modify:
[List each file path with specific changes]

**File**: `/path/to/file.rs`
- **Lines to change**: [specific line numbers]  
- **Current code pattern**: 
```rust
[exact current code from recommendation]
```
- **New code implementation**:
```rust  
[exact proposed code from recommendation]
```

### Integration with Existing Plan
- **Dependencies**: [Any prerequisites or dependencies on other recommendations]
- **Impact on existing sections**: [How this affects other parts of the plan]
- **Related components**: [Other components that interact with this change]

### Implementation Priority: [High/Medium/Low from recommendation]

### Verification Steps
1. Compile successfully after changes
2. Run existing tests
3. [Any specific validation steps]

---
**Design Review Decision**: Approved for inclusion in plan
**Next Steps**: Code changes ready for implementation when needed
```

### Final Summary

Use the `<FinalSummaryTemplate>` from @shared/keyword_review_pattern.txt with these parameters:
- **[REVIEW_TYPE]**: "Design Review"
- **[PRIMARY_ACTION_ITEMS]**: "Recommendations Agreed"
- **[PRIMARY_CATEGORY_BREAKDOWN]**: 
  ```
  - TYPE-SYSTEM: [count]
  - DESIGN: [count]
  - IMPLEMENTATION: [count]
  - SIMPLIFICATION: [count]
  ```
- **[SECONDARY_ACTION_ITEMS]**: "Recommendations Skipped"
- **[SECONDARY_ITEMS_LIST]**: "[List with brief descriptions]"
- **[MAINTAINED_ITEMS]**: "Deviations Accepted"
- **[MAINTAINED_ITEMS_DESCRIPTION]**: "[List with brief descriptions] - Plan document updated with notes"
- **[COMPLETION_STATEMENT]**: "Plan document is now ready for implementation."

## Default Review Target
**DEFAULT**: When not specified otherwise, the design reviewer should review and edit the plan markdown file currently being worked on. The subagent should be instructed to review this document and provide recommendations for improving it.
