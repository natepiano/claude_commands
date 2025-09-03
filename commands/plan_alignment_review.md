# Plan Alignment Review Command

## Arguments
**Required**: Plan document filename (e.g., `plan_enum_generation.md`, `plan_mutation_system.md`)
- The plan document that was used as the basis for implementation
- Must be a valid plan*.md file in the project root

## Overview
Use the Task tool with a general-purpose agent to verify that the actual implementation aligns with the specified plan document. This review produces **actionable discrepancies** between what was planned and what was implemented.

**CRITICAL**: After receiving the subagent's review, follow the 4-step workflow from @shared/post_review_workflow.txt:
1. **RECEIVE SUBAGENT'S REVIEW**: Get the structured findings with code examples
2. **PRESENT INITIAL SUMMARY**: Show summary statistics and overview table using `<CorePresentationFlow>` 
3. **EXECUTE PARALLEL INVESTIGATION**: Auto-investigate all findings with verdicts
4. **BEGIN KEYWORD-DRIVEN REVIEW**: Present each discrepancy one at a time with keywords

## Review Scope
- Compare the plan document's specifications against the actual implementation
- Identify missing planned features, unauthorized additions, and specification mismatches
- Focus on structural alignment, not code quality (that's covered by code_review.md)
- Provide concrete evidence of alignment or misalignment with file references
- **CRITICAL**: Do NOT include or evaluate any timing, schedules, or timelines from the plan

## Required Output Format

Use the standard output format from `<StandardOutputFormat>` in @shared/keyword_review_pattern.txt with these parameters:

**Summary Parameters:**
- **[REVIEW_SUMMARY_TITLE]**: "Alignment Summary"
- **[REVIEW_TARGET_DESCRIPTION]**: "alignment status"
- **[POSITIVE_METRIC_LABEL]**: "Fully aligned items"
- **[ACTION_METRIC_LABEL]**: "Discrepancies found" 
- **[POSITIVE_FINDINGS_SECTION_TITLE]**: "Well-Aligned Areas (Following Plan Correctly)"

**Additional Metrics for Plan Alignment:**
- ⚠️ Partially aligned items: [COUNT]
- ❌ Misaligned items: [COUNT]
- ➕ Unplanned additions: [COUNT]
- ➖ Missing implementations: [COUNT]

**Findings Parameters:**
- **[FINDINGS_SECTION_TITLE]**: "Alignment Discrepancies"
- **[PROBLEMS_DESCRIPTION]**: "alignment discrepancies"
- **[ACTION_TYPE]**: "alignment or plan updates"
- **[FINDINGS_TYPE]**: "discrepancies"

**Category Parameters:**
- **[CATEGORY-TYPE]**: MISSING-*, MISMATCH-*, PARTIAL-*, UNPLANNED-*, SPECIFICATION-*
- **[PRIMARY_FIELD_LABEL]**: Varies by category:
  - MISSING: "Planned Specification"
  - MISMATCH: "Planned Approach"
  - PARTIAL: "Planned Scope"
  - UNPLANNED: "Addition Description"
  - SPECIFICATION: "Specified Requirement"
- **[LOCATION_FIELD]**: "Plan Section" or "Location" 
- **[CURRENT_STATE_FIELD]**: "Current State" or "Actual Implementation"
- **[PROPOSED_CHANGE_FIELD]**: Based on discrepancy type (alignment needed vs plan update)
- **[IMPACT_FIELD_LABEL]**: "Impact" or "Fix Complexity"

**Plan Alignment Specific Requirements:**
- Include exact quotes from plan document with section references
- Provide actual code snippets or confirmation of absence
- Focus ONLY on plan-to-implementation alignment (not code quality)
- Acknowledge well-aligned areas in positive findings section

## Analysis Requirements

### Plan Document Analysis (MANDATORY FIRST STEP)
1. **Structured Reading**: Parse plan document section by section
2. **Feature Extraction**: List all promised features, APIs, and behaviors
3. **Specification Capture**: Note all technical requirements and constraints
4. **Dependency Mapping**: Understand planned component relationships
5. **Priority Identification**: Note which items were marked as critical vs optional

### Implementation Verification
1. **Feature Presence**: Check each planned feature exists in code
2. **API Matching**: Verify signatures match plan specifications
3. **Behavior Validation**: Confirm implementations follow planned algorithms
4. **Structure Comparison**: Check architectural alignment with plan
5. **Unplanned Detection**: Identify code not referenced in plan
6. **Build Warning Analysis**: Run `cargo build` and analyze warnings for alignment issues

### Deviation Classification
1. **Justified Deviations**: Implementation improvements discovered during coding
2. **Unjustified Deviations**: Arbitrary changes without clear benefit
3. **Technical Necessities**: Changes required by unforeseen constraints
4. **Scope Creep**: Additions beyond planned requirements
5. **Incomplete Work**: Planned items not yet implemented
6. **Unused Code Issues**: Code that exists but isn't called/used:
   - Planned features not yet integrated (partial implementation)
   - Dead code from refactoring (should be removed)
   - Preparatory code for future features (check if mentioned in plan)

## Prompt Template

Use the shared subagent prompt template from @shared/subagent_prompt_template.txt with these parameters:

- **[REVIEW_TARGET]**: "whether IMPLEMENTED CODE matches [PLAN DOCUMENT NAME]"
- **[OUTPUT_DESCRIPTION]**: "actionable discrepancies"
- **[REVIEW_CONTEXT]**: "You are checking if the ACTUAL IMPLEMENTATION matches what was planned."
- **[CONTEXT_DETAILS]**: 
  - The plan describes what SHOULD have been built
  - The code shows what WAS ACTUALLY built
  - Your job is to find where they don't match
  - You are NOT reviewing the quality of the plan itself
- **[TARGET_SELECTION_INSTRUCTIONS]**: "Read the complete plan document: `$ARGUMENTS` and compare against actual implementation"
- **[STEP_1_LABEL]**: "MANDATORY FIRST STEP - READ PLAN DOCUMENT"
- **[STEP_1_INSTRUCTIONS]**: 
  1. Read the complete plan document: `$ARGUMENTS`
  2. Extract and list ALL planned features, specifications, and requirements
  3. Create a mental checklist of what should exist according to the plan
  4. Note any sections marked as "optional" or "future work"
- **[STEP_2_LABEL]**: "MANDATORY SECOND STEP - MAP IMPLEMENTATION"
- **[STEP_2_INSTRUCTIONS]**: 
  1. Search the codebase for each planned feature
  2. Read the actual implementation files
  3. Compare actual code against plan specifications
  4. Look for code that wasn't mentioned in the plan
- **[STEP_3_LABEL]**: "CRITICAL THIRD STEP - BUILD WARNING ANALYSIS"
- **[STEP_3_INSTRUCTIONS]**: 
  ```
  1. Run `cargo build 2>&1 | grep -E "warning:|error:"` to identify all warnings
  2. For each warning about unused code (functions, structs, fields, etc.), determine:
     - **Planned but not integrated**: Code was written for a planned feature but not yet connected
       → Flag as PARTIAL-* discrepancy with details about what's missing
     - **Obsolete from refactoring**: Code became unused during implementation changes
       → Flag as UNPLANNED-* for removal
     - **Placeholder for future**: Code is intentionally unused, waiting for other features
       → Check if plan mentions this as preparatory work
  3. Common warning patterns to analyze:
     - `field is never read` → Check if field should be used per plan
     - `function is never used` → Verify if function should be called per plan
     - `variant is never constructed` → Confirm if enum variant should be used per plan
     - `struct is never constructed` → Determine if type should be instantiated per plan
  ```
- **[STEP_4_LABEL]**: "CRITICAL FOURTH STEP - ALIGNMENT ANALYSIS"
- **[STEP_4_INSTRUCTIONS]**: 
  ```
  For each planned item, determine:
  - **Fully Aligned**: Implementation matches plan exactly
  - **Partially Aligned**: Some aspects match, others don't (including unused code for planned features)
  - **Misaligned**: Implementation contradicts plan
  - **Missing**: Plan item not implemented at all
  - **Unplanned**: Implementation has no plan reference (including dead code to remove)
  ```
- **[FINAL_FILTER_LABEL]**: "EVIDENCE REQUIREMENTS"
- **[FINAL_FILTER_INSTRUCTIONS]**: 
  ```
  Every discrepancy must include:
  1. Exact quote or section reference from plan document
  2. Actual code snippet or confirmation of absence
  3. Specific file paths and line numbers
  4. Clear explanation of the discrepancy
  ```
- **[SHARED_ANALYSIS_REFERENCE]**: "TYPE SYSTEM ANALYSIS"
- **[SHARED_REFERENCE_INSTRUCTIONS]**: "Follow `<TypeSystemDesignPrinciples>` from the shared pattern file when analyzing alignment issues."
- **[STANDARD_ANALYSIS_STEPS]**: 
  1. Comparing plan specifications against actual implementation
  2. Identifying missing planned features
  3. Finding unauthorized additions beyond plan scope
  4. Detecting specification mismatches and deviations
  5. Classifying alignment status for each planned component
- **[REVIEW_SPECIFIC_REQUIREMENTS]**: 
  - **IMPORTANT FILTERS**:
    - Ignore code quality issues (covered by code_review.md)
    - Ignore plan quality issues (covered by design_review.md)
    - Focus ONLY on plan-to-implementation alignment
    - Don't flag intentional plan sections marked "future work" or "optional"
    - Don't flag standard boilerplate (tests, imports, etc.) as unplanned
    - IGNORE any timing, schedule, or timeline information in the plan - focus only on technical specifications
- **[OUTPUT_FORMAT_REQUIREMENTS]**: "Return results in the structured format with MISSING-*, MISMATCH-*, PARTIAL-*, UNPLANNED-*, and SPECIFICATION-* categories."
- **[PRIORITY_INSTRUCTIONS]**: "Each finding must be actionable - provide enough detail for immediate correction."
- **[DETAILED_REQUIREMENTS]**: "Include positive findings in the \"Well-Aligned Areas\" section to acknowledge good adherence."

## Post-Review Instructions - Auto-Investigation and Alignment Process

Follow the 4-step workflow from @shared/post_review_workflow.txt with these parameters:
- **[REVIEW_TYPE]**: "Plan Alignment Review"
- **[FINDINGS_TYPE]**: "alignment discrepancy findings"
- **[ITEMS]**: "discrepancies"
- **[ITEM]**: "discrepancy"
- **[RESOLUTION_STATE]**: "already resolved by prior alignments"
- **[DOMAIN_VALUES]**: "plan adherence AND pragmatism"
- **[INVESTIGATION_FOCUS_LIST]**:
  - Understand why the implementation diverged from plan
  - Evaluate technical merit of both approaches
  - Check if implementation discovered improvements
  - Assess complexity/risk of alignment effort
  - Consider unforeseen technical constraints
  - Analyze business/functional impact
  - Determine if plan should be updated instead
- **[EXPECTED_VERDICTS]**: "ALIGN RECOMMENDED, ACCEPT RECOMMENDED, or DEFER RECOMMENDED"
- **[UPDATE_TARGET]**: "both code and plan updates"
- **[CATEGORY_SET]**: "alignment categories"

### Alignment Review Specific Keywords (Based on Verdict)
**For ALIGN RECOMMENDED verdict**:
- `align to plan` - Modify code to match plan
- `skip` - Defer to TECH_DEBT.md
- `accept as built` - Update plan to match implementation
- `investigate further` - Get targeted deeper analysis

**For ACCEPT RECOMMENDED verdict**:
- `accept as built` - Update plan to match implementation
- `align to plan` - Modify code anyway
- `skip` - Defer decision
- `investigate further` - Get targeted deeper analysis

**For DEFER RECOMMENDED verdict**:
- `skip` - Add to TECH_DEBT.md
- `align to plan` - Align now despite recommendation
- `accept as built` - Accept deviation instead
- `investigate further` - Get targeted deeper analysis

### Alignment Review Keyword Response Actions

Follow `<KeywordResponseWorkflow>` from @shared/keyword_review_pattern.txt with these parameters:
- **[ITEM]**: "discrepancy"
- **[CUMULATIVE_REVIEW_SPECIFICS]**: "Update remaining discrepancies to account for the cumulative changes - some mismatches may now be resolved, line numbers may have shifted, and new considerations may apply."

**Review-Specific Actions**:

**When user responds "align to plan"**:
**[REVIEW_SPECIFIC_ACTION]**: **IMPLEMENT ALIGNMENT**: 
1. Modify the code to match the plan specification
2. Show the implemented change with before/after comparison

**When user responds "skip"**:
**[REVIEW_SPECIFIC_ACTION]**: Add to skipped items list (in memory for final summary)

**When user responds "accept as built"**:
**[REVIEW_SPECIFIC_ACTION]**: **UPDATE PLAN DOCUMENT**: Add deviation note to the plan:
```markdown
### Implementation Note: [DISCREPANCY-ID]
**Deviation from Original Plan**
- **Original**: [What was planned]
- **Actual**: [What was implemented]
- **Rationale**: Accepted deviation - [reason if provided]
- **Status**: Accepted
```

### Final Summary

Use the `<FinalSummaryTemplate>` from @shared/keyword_review_pattern.txt with these parameters:
- **[REVIEW_TYPE]**: "Plan Alignment Review" 
- **[PRIMARY_ACTION_ITEMS]**: "Discrepancies Aligned"
- **[PRIMARY_CATEGORY_BREAKDOWN]**: 
  ```
  - MISSING: [count] items now implemented
  - MISMATCH: [count] items corrected
  - SPECIFICATION: [count] items fixed
  ```
- **[SECONDARY_ACTION_ITEMS]**: "Discrepancies Skipped"
- **[SECONDARY_ITEMS_LIST]**: "[List with brief descriptions]"
- **[MAINTAINED_ITEMS]**: "Well-Aligned Areas Maintained" 
- **[MAINTAINED_ITEMS_DESCRIPTION]**: "[Quick recap of areas that matched plan well]"
- **[COMPLETION_STATEMENT]**: "Plan and implementation are now synchronized."

**Additional Summary Sections:**
- **Deviations Accepted**: [Z] - [List with brief descriptions] - Plan document updated with notes
- **Unplanned Additions**: [W] - Removed: [count], Kept & Documented: [count]

## Plan Document Cleanup

After completing the plan alignment review and all discrepancies are resolved:
- Ask the user if it is okay to remove the plan document
- If approved, delete the plan document file and run `git rm` on it

## Relationship to Other Commands

- **After `task_a_subagent.md`**: Use this to verify implementation matches plan
- **Before `code_review.md`**: Ensure structural alignment before quality review
- **Complements `design_review.md`**: That reviews the plan, this reviews plan adherence
- **Tracks skipped items**: Skipped alignments are listed in the final summary