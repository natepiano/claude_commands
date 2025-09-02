# Actionable Design Review Command

## Arguments
If `$ARGUMENTS` is provided, it should be the name of a `plan*.md` file in the project root that will be the target of the design review. For example:
- `plan_enum_generation.md`
- `plan_mutation_system.md`
- `plan_api_redesign.md`

If no arguments are provided, the design review will target the plan markdown document currently being worked on.

## Overview
Use the Task tool with a general-purpose agent to conduct a comprehensive design review that produces **actionable recommendations for discussion**. Be sure to instruct the agent to think hard about the items in the <ReviewScope/>. The primary goal is to edit the plan markdown file being reviewed with approved suggestions and track skipped items.

**CRITICAL**: After receiving the subagent's review, create a todo list for interactive review. Present each recommendation one at a time, STOPPING after each one for the user to decide whether to skip or implement it.

## Review Scope
- Analyze the current implementation plan for completeness and feasibility
- Identify gaps, missing components, and implementation issues
- Focus on breaking changes, over-engineering, and simplification opportunities
- Provide concrete, specific recommendations that can be turned into todo items
- **CRITICAL**: Do NOT include any timing, schedules, timelines, or time estimates in recommendations

## Required Output Format

The subagent must return recommendations in this **exact format** for easy todo list creation:

### Summary
Brief overview of findings (2-3 sentences max)

### Actionable Recommendations

**DESIGN-[ID]**: [Brief title of recommendation]
- **Issue**: [What specific problem this addresses]
- **Recommendation**: [Specific action to take - include where in the plan document to add/edit]
- **Files Affected**: [Plan document section and/or code file paths]
- **Priority**: [High/Medium/Low]
- **Rationale**: [Why this change improves the design]
- **Implementation Proposal**:
  ```rust
  // Current:
  [Show actual current code from the file]
  
  // Proposed:
  [Show exactly what the code should look like after the change]
  ```

**IMPLEMENTATION-[ID]**: [Brief title of recommendation]
- **Issue**: [Implementation gap or complexity issue]
- **Recommendation**: [Specific implementation change]
- **Files Affected**: [Exact file paths]
- **Priority**: [High/Medium/Low]
- **Dependencies**: [What must be done first, if anything]
- **Implementation Proposal**:
  ```rust
  // Current:
  [Show actual current code from the file, or "// Missing implementation" if gap]
  
  // Proposed:
  [Show exactly what the code should look like after the change]
  ```

**SIMPLIFICATION-[ID]**: [Brief title of recommendation]
- **Issue**: [Over-engineered or complex area]
- **Recommendation**: [How to simplify while preserving functionality]
- **Files Affected**: [Exact file paths]
- **Priority**: [High/Medium/Low]
- **Benefits**: [What this simplification achieves]
- **Implementation Proposal**:
  ```rust
  // Current:
  [Show actual current complex code from the file]
  
  // Proposed:
  [Show simplified version that preserves functionality]
  ```

**TYPE-SYSTEM-[ID]**: [Brief title - e.g., "Replace conditional chain with enum"]
- **Issue**: [Specific conditional or function that violates type system principles]
- **Current Code Pattern**: [Brief description of the problematic pattern]
- **Proposed Type Design**: [Specific enum/trait/struct to introduce]
- **Files Affected**: [Exact file paths]
- **Priority**: [High - these are ALWAYS high priority]
- **Implementation Proposal**:
  ```rust
  // Current:
  [Show actual current conditional/stringly-typed code from the file]
  
  // Proposed:
  [Show the type-driven approach with enums/pattern matching]
  ```

## Analysis Requirements

### Type System Architecture (MANDATORY FIRST ANALYSIS)
1. **Conditional Audit**: Every if-else chain is a design failure. Flag ALL conditionals that could be:
   - Replaced with enum variants and pattern matching
   - Eliminated through trait implementations
   - Removed by making illegal states unrepresentable
2. **Function Audit**: Every standalone utility function is suspect. Flag functions that should be:
   - Methods on a type that owns the behavior
   - Trait implementations for polymorphic behavior
   - Eliminated by better type design
3. **String Typing**: Every string that represents a finite set of values should be an enum
4. **State Machines**: Any code tracking state with booleans/strings should use state enums
5. **Builder Pattern**: Complex construction logic should use builders, not functions with many parameters

### Standard Analysis
1. **Gap Analysis**: Missing components, unclear specifications
2. **Breaking Changes**: API changes, signature modifications, structural changes
3. **Over-engineering**: Unnecessary complexity that can be simplified
4. **Implementation Order**: Dependencies and sequencing issues (NO time estimates)
5. **Code Examples**: Only when essential for clarity (prefer brief descriptions)
6. **NO TIMING**: Never include schedules, timelines, deadlines, or time estimates

## Prompt Template
```
Task a general-purpose subagent to review [REVIEW TARGET: plan markdown document (default), specific files, or focus area] and provide actionable recommendations in the exact format specified above.

**TARGET SELECTION**: 
- If `$ARGUMENTS` is provided, review the specified `plan*.md` file in the project root
- If no arguments are provided, review the plan markdown document currently being worked on
- Include specific section names where edits should be made in the target document

**MANDATORY FIRST STEP - CHECK SKIP NOTES**:
<SkipNotesCheck>
1. **IMMEDIATELY** look for a "Design Review Skip Notes" section in the document
2. **READ AND MEMORIZE** all skipped items - these are OFF LIMITS
3. **PAY SPECIAL ATTENTION** to items marked with "⚠️ PREJUDICE WARNING" - suggesting these again is a CRITICAL FAILURE
4. **DO NOT PROCEED** until you have confirmed what has been previously skipped
</SkipNotesCheck>

**MANDATORY SECOND STEP - UNDERSTAND THE REVIEW SCOPE**:
<ReviewScope>
1. Carefully read and understand what you're reviewing (plan, files, or specific focus area)
2. Note what is already present, implemented, or explicitly planned in the review scope
3. DO NOT suggest things that are already part of what you're reviewing
4. DO NOT suggest anything that appears in the Skip Notes section
5. Focus on genuine gaps, improvements, and issues not already addressed
</ReviewScope>

**CRITICAL THIRD PASS - TYPE SYSTEM VIOLATIONS**:
Before ANY other analysis, audit for type system misuse:
- Every if-else chain checking type/kind/variant strings should be an enum with pattern matching
- Every utility function should be questioned - why isn't this a method on a type?
- Every boolean flag tracking state should be part of a state enum
- Every stringly-typed API should use proper types
- Every deeply nested conditional is a failure to model the domain

TREAT CONDITIONALS AS THE ENEMY. Each conditional is a missed opportunity to use the type system.

Then proceed with standard analysis:
1. Identifying ACTUAL gaps and missing components (not things already present in the review scope)
2. Finding breaking changes not explicitly called out
3. Spotting over-engineering and unnecessary complexity
4. Ensuring implementation order makes sense
5. Creating concrete, discussable recommendations for improvements NOT already addressed

**IMPORTANT FILTER**: Before including any recommendation:
1. **FIRST**: Verify it's not in the Skip Notes section (especially ⚠️ PREJUDICE WARNING items)
2. **SECOND**: Verify it's not already handled in what you're reviewing
3. **THIRD**: Only suggest genuine improvements and additions NOT already addressed

**CRITICAL**: Suggesting something from the Skip Notes section, especially items with ⚠️ PREJUDICE WARNING, constitutes a review failure and wastes everyone's time.

Return results in the structured format with TYPE-SYSTEM-*, DESIGN-*, IMPLEMENTATION-*, and SIMPLIFICATION-* categories. TYPE-SYSTEM recommendations should come FIRST and be treated as highest priority.

**CRITICAL REMINDER**: Do NOT include any timing information in recommendations:
- NO schedules, deadlines, or milestones
- NO time estimates or durations
- NO timeline or project phases with dates
- Focus ONLY on technical design and implementation structure

**CRITICAL - IMPLEMENTATION PROPOSALS REQUIRED**: For each recommendation, you MUST include an "Implementation Proposal" section with concrete code examples:
- **Current**: Show the actual existing code from the relevant files (read the files to get real code)
- **Proposed**: Show exactly what the code should look like after implementing the recommendation
- Use proper syntax highlighting with ```rust code blocks
- For implementation gaps, use "// Missing implementation" in the Current section
- Make the code examples specific and actionable, not pseudo-code

For each recommendation, specify the exact location in the plan document where it should be added or edited (e.g., "Add to Section 3.2 Implementation Details" or "Edit the API Design section").

[INSERT REVIEW TARGET AND ANY CUSTOM INSTRUCTIONS HERE]
```

## Post-Review Instructions - Keyword-Driven Implementation Process

**CRITICAL**: Read `~/.claude/commands/shared/keyword_review_pattern.txt` and follow the shared patterns with these customizations:

### Pattern Application for Design Review
- Follow `<CorePresentationFlow>` with [Review Type] = "Design Review" and [items] = "recommendations"
- Follow `<KeywordDecisionProcess>` with keywords: agree, skip, investigate, skip with prejudice
- Follow `<InvestigationPattern>` with [DOMAIN VALUES] = "code elegance AND practical utility"
- Follow `<CumulativeUpdateRule>` for plan document updates
- Follow `<EnforcementRules>` completely
- Use `<FinalSummaryTemplate>` with appropriate design review categories

### Design Review Specific Keywords
**Primary keywords**: agree, skip, investigate, skip with prejudice
**After investigation**: agree, skip, skip with prejudice, override (for REJECTED verdict only)

### Design Review Keyword Response Actions

**When user responds "agree"**:
1. **UPDATE PLAN DOCUMENT ONLY**: Add a new dedicated section for the agreed recommendation with:
   - Full implementation details from the recommendation
   - Specific code changes required (to be implemented later)
   - File paths and line numbers to modify (for future reference)
   - Step-by-step implementation instructions (for when implementation begins)
   - Integration points with existing plan sections
2. **CROSS-REFERENCE**: Add references between the new plan section and related implementation sections
3. **Mark current todo as completed**
4. **CRITICAL**: Before presenting the next recommendation, review ALL changes accepted in this session so far. Update the suggestion to account for the cumulative effect of these changes - leverage newly added patterns, avoid redundant suggestions, and ensure consistency with what has already been approved.
5. Present next recommendation, then EXPLICITLY state the available keywords:
   ```
   Please respond with one of these keywords:
   - "agree" - ACTION: Add this recommendation to the plan document as an approved item
   - "skip" - ACTION: Reject this recommendation and add it to Skip Notes
   - "investigate" - ACTION: Launch deep analysis to validate if this is worth implementing
   - "skip with prejudice" - ACTION: Permanently reject with ⚠️ warning against future suggestions
   ```
   Then STOP

**When user responds "skip"**:
1. **UPDATE SKIP NOTES**: Add/update "Design Review Skip Notes" section in the plan document using this EXACT format:
   ```markdown
   ## Design Review Skip Notes
   
   ### [RECOMMENDATION-ID]: [Title]
   - **Status**: SKIPPED
   - **Category**: [TYPE-SYSTEM/DESIGN/IMPLEMENTATION/SIMPLIFICATION]
   - **Description**: [Brief description of what was skipped]
   - **Reason**: User decision - not needed for current implementation
   ```
2. **Mark current todo as completed**
3. **CRITICAL**: Before presenting the next recommendation, review ALL changes accepted/skipped in this session so far. Ensure you don't re-suggest anything already addressed or marked as skipped.
4. Present next recommendation, then EXPLICITLY state the available keywords:
   ```
   Please respond with one of these keywords:
   - "agree" - ACTION: Add this recommendation to the plan document as an approved item
   - "skip" - ACTION: Reject this recommendation and add it to Skip Notes
   - "investigate" - ACTION: Launch deep analysis to validate if this is worth implementing
   - "skip with prejudice" - ACTION: Permanently reject with ⚠️ warning against future suggestions
   ```
   Then STOP

**When user responds "skip with prejudice"**:
1. **UPDATE SKIP NOTES WITH PREJUDICE FLAG**: 
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
2. **Mark current todo as completed**
3. **CRITICAL**: Before presenting the next recommendation, review ALL changes accepted/skipped/rejected in this session so far. Never suggest anything marked with prejudice or already handled.
4. Present next recommendation, then EXPLICITLY state the available keywords:
   ```
   Please respond with one of these keywords:
   - "agree" - ACTION: Add this recommendation to the plan document as an approved item
   - "skip" - ACTION: Reject this recommendation and add it to Skip Notes
   - "investigate" - ACTION: Launch deep analysis to validate if this is worth implementing
   - "skip with prejudice" - ACTION: Permanently reject with ⚠️ warning against future suggestions
   ```
   Then STOP

**When user responds "investigate"**:
1. **TASK INVESTIGATION AGENT**: Use Task tool to deep-dive investigate the recommendation with a general-purpose agent acting as a **responsible judge balancing aesthetics and utility**:
   - **CRITICAL JUDGMENT MANDATE**: Act as a responsible engineering judge who values both code elegance AND practical utility
   - **Balance aesthetics vs pragmatism**: Weigh the beauty of clean abstractions against real-world implementation costs
   - **Provide sound engineering judgment**: Consider maintenance burden, team cognitive load, and actual business value
   - **Analyze whether the recommendation provides genuine value vs over-engineering**: Be skeptical of complexity for complexity's sake
   - **Research alternative approaches and trade-offs**: Find the sweet spot between perfectionism and pragmatism
   - **Examine real-world impact and complexity costs**: Consider developer time, debugging difficulty, and onboarding friction
   - **Check if simpler solutions exist that achieve the same goals**: Sometimes "good enough" is better than perfect
   - **Provide evidence-based assessment of necessity**: Ground your judgment in concrete examples and real scenarios
2. **PRESENT INVESTIGATION FINDINGS**: Return with:
   - **Value Assessment**: Clear judgment on whether this is worth implementing
   - **Alternative Approaches**: If multiple valid approaches exist, present 2-3 options with trade-offs
   - **Complexity Analysis**: Honest assessment of implementation and maintenance costs
   - **Investigation Verdict**: One of:
     - **RECOMMENDATION CONFIRMED**: Investigation supports implementing the original recommendation
     - **RECOMMENDATION MODIFIED**: Investigation suggests a modified approach (specify the changes)
     - **RECOMMENDATION REJECTED**: Investigation recommends NOT implementing this suggestion
3. **WAIT FOR NEW KEYWORD WITH CONTEXT-APPROPRIATE MEANING**: Present findings and then EXPLICITLY state based on the investigation verdict:
   
   **If RECOMMENDATION CONFIRMED**:
   ```
   Investigation verdict: CONFIRMED - Investigation supports implementing this recommendation
   
   Please respond with one of these keywords:
   - "agree" - ACTION: Add recommendation to plan document as approved implementation item
   - "skip" - ACTION: Reject recommendation and add to Skip Notes despite investigation support
   - "skip with prejudice" - ACTION: Permanently reject with warning in Skip Notes
   ```
   
   **If RECOMMENDATION MODIFIED**:
   ```
   Investigation verdict: MODIFIED - Investigation suggests implementing with changes: [describe changes]
   
   Please respond with one of these keywords:
   - "agree" - ACTION: Add MODIFIED version to plan document as approved implementation item
   - "skip" - ACTION: Reject both original and modified versions, add to Skip Notes
   - "skip with prejudice" - ACTION: Permanently reject all versions with warning in Skip Notes
   ```
   
   **If RECOMMENDATION REJECTED**:
   ```
   Investigation verdict: REJECTED - Investigation recommends NOT implementing this suggestion
   Reason: [specific reason from investigation]
   
   Please respond with one of these keywords:
   - "agree" - ACTION: Accept investigation's rejection, add to Skip Notes as "Investigated and Rejected"
   - "override" - ACTION: Ignore investigation results, add original recommendation to plan anyway
   - "skip with prejudice" - ACTION: Permanently reject with extra warning in Skip Notes
   ```
   
4. **KEYWORD ACTIONS AFTER INVESTIGATION** (detailed behaviors):
   - **"agree" after CONFIRMED verdict**: 
     - Add recommendation to plan document with "DESIGN REVIEW AGREEMENT" section
     - Mark as investigated and approved
     - Include investigation findings as justification
   - **"agree" after MODIFIED verdict**: 
     - Add MODIFIED version to plan document
     - Include investigation's proposed changes
     - Document why modifications were needed
   - **"agree" after REJECTED verdict**: 
     - Add to Skip Notes with special "Investigated and Rejected" status
     - Include investigation's reasoning for rejection
     - This is functionally equivalent to "skip" but documents the investigation
   - **"override" (only after REJECTED verdict)**: 
     - Add original recommendation to plan despite negative investigation
     - Document that investigation advised against but was overridden
     - Include both investigation findings and override rationale
   - **"skip"**: 
     - Always means reject the recommendation
     - Add to Skip Notes as regular skip
   - **"skip with prejudice"**: 
     - Always means permanently reject with ⚠️ PREJUDICE WARNING
     - Add strong warning against future suggestions

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
**Design Review Decision**: Approved for inclusion in plan on [current date]
**Next Steps**: Code changes ready for implementation when needed
```

## Default Review Target
**DEFAULT**: When not specified otherwise, the design reviewer should review and edit the plan markdown file currently being worked on. The subagent should be instructed to review this document and provide recommendations for improving it.
