# Actionable Code Review Command

## Arguments
- **Default (no arguments)**: Reviews all uncommitted changes via `git diff`
- **With file/directory paths**: Reviews all code in specified files or directories (as-built review)
  - Example: `/code_review src/parser/*.rs` - reviews all code in parser files
  - Example: `/code_review lib/utils.js tests/` - reviews all code in utils.js and all test files

## Overview
Use the Task tool with a general-purpose agent to conduct a comprehensive code review of the specified scope. The review produces **actionable recommendations for immediate fixing**.

**CRITICAL**: After receiving the subagent's review, follow the 4-step workflow from @shared/post_review_workflow.txt:
1. **RECEIVE SUBAGENT'S REVIEW**: Get the structured findings with code examples
2. **PRESENT INITIAL SUMMARY**: Show summary statistics and overview table using `<CorePresentationFlow>` 
3. **EXECUTE PARALLEL INVESTIGATION**: Auto-investigate all findings with verdicts
4. **BEGIN KEYWORD-DRIVEN REVIEW**: Present each issue one at a time with keywords

## Review Scope
- **Default**: Analyze the current git diff (uncommitted changes)
- **With arguments**: Analyze all code in specified files/directories (as-built review)
- Identify code quality issues, complexity, duplication, and type system misuse
- Focus on actionable improvements that can be immediately fixed
- Provide concrete, specific recommendations with code samples

## Required Output Format

Use the standard output format from `<StandardOutputFormat>` in @shared/keyword_review_pattern.txt with these parameters:

**Summary Parameters:**
- **[REVIEW_SUMMARY_TITLE]**: "Review Summary"
- **[REVIEW_TARGET_DESCRIPTION]**: "changes reviewed"
- **[POSITIVE_METRIC_LABEL]**: "Positive findings"
- **[ACTION_METRIC_LABEL]**: "Suggested fixes"
- **[POSITIVE_FINDINGS_SECTION_TITLE]**: "Positive Findings (Good Practices Observed)"

**Findings Parameters:**
- **[FINDINGS_SECTION_TITLE]**: "Actionable Issues"
- **[PROBLEMS_DESCRIPTION]**: "actual problems"
- **[ACTION_TYPE]**: "fixing"
- **[FINDINGS_TYPE]**: "issues"

**Category Parameters:**
- **[CATEGORY-TYPE]**: TYPE-SYSTEM-*, QUALITY-*, COMPLEXITY-*, DUPLICATION-*, SAFETY-*
- **[PRIMARY_FIELD_LABEL]**: "Issue" (consistent across all categories)
- **[LOCATION_FIELD]**: "Location"
- **[CURRENT_STATE_FIELD]**: "Current Code" or "Current Pattern"
- **[PROPOSED_CHANGE_FIELD]**: "Suggested Code" or "Suggested Refactor"
- **[IMPACT_FIELD_LABEL]**: Varies by category:
  - TYPE-SYSTEM/QUALITY/COMPLEXITY: "Impact" 
  - DUPLICATION: "Benefits"
  - SAFETY: "Risk"

**Code Review Specific Requirements:**
- Include exact file paths and line numbers for all issues
- Provide actual code snippets for current and suggested versions  
- Make suggestions immediately implementable
- Only flag bare `.unwrap()` calls (not `unwrap_or()` variants)
- TYPE-SYSTEM issues are ALWAYS high priority

## Known Issues (Do NOT Flag These)

### Acceptable Patterns
1. **Safe unwrap variants**: `unwrap_or()` and `unwrap_or_else()` are acceptable as they provide defaults
2. **Only flag bare `.unwrap()`**: Only suggest fixes for bare `.unwrap()` calls without fallbacks
3. **String validation patterns**: Format validation (email, URLs, etc.) and arbitrary text processing is acceptable
4. **String accessors from typed data**: Methods like `.name()` on well-typed enums that extract string fields are acceptable
5. **String data in type-safe wrappers**: Strings already contained within proper type-safe structures don't need further wrapping

## Analysis Requirements

**CRITICAL**: Read and follow @shared/keyword_review_pattern.txt section `<TypeSystemDesignPrinciples>` for comprehensive analysis guidance.

### Code Review Specific Analysis
Beyond the shared principles, also analyze:
1. **Code Quality**: Naming, formatting, idioms, best practices
2. **Documentation**: Missing or incorrect comments for complex logic
3. **Test Coverage**: Areas that need better test coverage

## Prompt Template

Use the shared subagent prompt template from @shared/subagent_prompt_template.txt with these parameters:

- **[REVIEW_TARGET]**: "ACTUAL CODE [REVIEW TARGET: git diff or specific files]"
- **[OUTPUT_DESCRIPTION]**: "actionable code review feedback"
- **[REVIEW_CONTEXT]**: "You are reviewing ACTUAL CODE for quality issues, NOT a plan."
- **[CONTEXT_DETAILS]**: 
  - You're looking at real implementation code
  - Your job is to find bugs, quality issues, and improvements IN THE CODE
  - You are NOT comparing against a plan or reviewing documentation
- **[TARGET_SELECTION_INSTRUCTIONS]**: 
  - If `$ARGUMENTS` is provided: Review all code in specified files/directories (as-built review)
  - If no arguments: Review all uncommitted changes via `git diff`
  - Use Read tool to examine all files when specific paths are provided
- **[STEP_1_LABEL]**: "FIRST STEP - Get the code"
- **[STEP_1_INSTRUCTIONS]**: 
  - If no arguments: Run `git diff` to get uncommitted changes
  - If arguments provided: Use Read tool to examine all code in specified files/directories
- **[STEP_2_LABEL]**: "CRITICAL SECOND STEP - TYPE SYSTEM VIOLATIONS"
- **[STEP_2_INSTRUCTIONS]**: "Follow `<TypeSystemDesignPrinciples>` from the shared pattern file exactly. This includes all primary type system violations, error handling standards, and what NOT to flag."
- **[SHARED_ANALYSIS_REFERENCE]**: "CRITICAL SECOND STEP - TYPE SYSTEM VIOLATIONS"
- **[SHARED_REFERENCE_INSTRUCTIONS]**: "Follow `<TypeSystemDesignPrinciples>` from the shared pattern file exactly. This includes all primary type system violations, error handling standards, and what NOT to flag."
- **[STANDARD_ANALYSIS_STEPS]**: 
  1. Identifying code quality issues in the code
  2. Finding unnecessary complexity that can be simplified
  3. Spotting code duplication
  4. Checking error handling and safety
  5. Creating concrete, fixable recommendations
- **[REVIEW_SPECIFIC_REQUIREMENTS]**: 
  - For git diff mode: Only review code that appears in the diff
  - For as-built mode: Review all code in specified files/directories
  - Provide actual code snippets for current and suggested versions
  - Make suggestions that can be immediately implemented
  - Prioritize issues by their impact on code quality and maintainability
- **[OUTPUT_FORMAT_REQUIREMENTS]**: 
  1. Review Summary with metrics and positive findings listed as bullet points
  2. ONLY actionable issues in TYPE-SYSTEM-*, QUALITY-*, COMPLEXITY-*, DUPLICATION-*, and SAFETY-* categories
  3. Do NOT include positive examples as issues - capture them ONLY in the Positive Findings section
- **[PRIORITY_INSTRUCTIONS]**: "TYPE-SYSTEM recommendations should come FIRST and be treated as highest priority."
- **[DETAILED_REQUIREMENTS]**: "Each issue must include the exact location, current code, and suggested code to make fixes straightforward."

## Post-Review Instructions - Auto-Investigation and Fix Process

Follow the 4-step workflow from @shared/post_review_workflow.txt with these parameters:
- **[REVIEW_TYPE]**: "Code Review"
- **[FINDINGS_TYPE]**: "code review findings"
- **[ITEMS]**: "issues"
- **[ITEM]**: "issue"
- **[RESOLUTION_STATE]**: "already fixed by previous changes"
- **[DOMAIN_VALUES]**: "code quality AND pragmatism"
- **[INVESTIGATION_FOCUS_LIST]**:
  - Assess actual bug risk vs stylistic preference
  - Evaluate fix complexity and refactoring scope
  - Consider impact on code clarity and readability
  - Check for performance implications
  - Analyze maintenance burden reduction
  - Verify fix won't introduce new issues
  - Consider alignment with team conventions
- **[EXPECTED_VERDICTS]**: "FIX RECOMMENDED, FIX MODIFIED, or FIX NOT RECOMMENDED"
- **[UPDATE_TARGET]**: "code changes (line numbers, resolved issues)"
- **[CATEGORY_SET]**: "issue categories"

### Code Review Specific Keywords (Based on Verdict)
**For FIX RECOMMENDED/MODIFIED verdicts**:
- `fix` - Implement the fix immediately
- `skip` - Don't fix this issue
- `investigate further` - Get targeted deeper analysis

**For FIX NOT RECOMMENDED verdicts**:
- `accept` - Accept recommendation to not fix
- `override` - Fix anyway despite recommendation
- `investigate further` - Get targeted deeper analysis

### Code Review Keyword Response Actions

Follow `<KeywordResponseWorkflow>` from @shared/keyword_review_pattern.txt with these parameters:
- **[ITEM]**: "issue"  
- **[CUMULATIVE_REVIEW_SPECIFICS]**: "Update remaining issues to account for the cumulative changes - adjust line numbers, remove issues already resolved by previous fixes, and modify suggestions to align with the current state of the code."

**Review-Specific Actions**:

**When user responds "fix"**:
**[REVIEW_SPECIFIC_ACTION]**: **IMPLEMENT IMMEDIATELY**: 
1. Apply the suggested code change
2. **RUN BUILD AND FORMAT**: Execute `cargo build && cargo +nightly fmt` to ensure the fix works
3. Show the implemented change with a brief confirmation

**When user responds "skip"**:
**[REVIEW_SPECIFIC_ACTION]**: Add to skipped issues list (in memory for final summary)

**When user responds "accept"** (for FIX NOT RECOMMENDED verdicts):
**[REVIEW_SPECIFIC_ACTION]**: Accept recommendation to not fix

**When user responds "override"** (for FIX NOT RECOMMENDED verdicts):
**[REVIEW_SPECIFIC_ACTION]**: **IMPLEMENT ANYWAY**: 
1. Apply the suggested code change despite recommendation
2. **RUN BUILD AND FORMAT**: Execute `cargo build && cargo +nightly fmt` to ensure the fix works
3. Show the implemented change with a brief confirmation

### Final Summary

Use the `<FinalSummaryTemplate>` from @shared/keyword_review_pattern.txt with these parameters:
- **[REVIEW_TYPE]**: "Code Review"
- **[PRIMARY_ACTION_ITEMS]**: "Issues Fixed"
- **[PRIMARY_CATEGORY_BREAKDOWN]**: 
  ```
  - TYPE-SYSTEM: [count]
  - QUALITY: [count]
  - COMPLEXITY: [count]
  - SAFETY: [count]
  - DUPLICATION: [count]
  ```
- **[SECONDARY_ACTION_ITEMS]**: "Issues Skipped"
- **[SECONDARY_ITEMS_LIST]**: "[List of skipped issue IDs with brief descriptions]"
- **[MAINTAINED_ITEMS]**: "Positive Practices Maintained"
- **[MAINTAINED_ITEMS_DESCRIPTION]**: "[Quick recap of positive findings]"
- **[COMPLETION_STATEMENT]**: "All changes have been applied to your working directory."

**Note**: The key difference from design_review.md is that this implements fixes immediately in the codebase rather than updating a plan document, making it a live fixing session.