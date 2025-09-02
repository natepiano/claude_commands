# Plan Alignment Review Command

## Arguments
**Required**: Plan document filename (e.g., `plan_enum_generation.md`, `plan_mutation_system.md`)
- The plan document that was used as the basis for implementation
- Must be a valid plan*.md file in the project root

## Overview
Use the Task tool with a general-purpose agent to verify that the actual implementation aligns with the specified plan document. This review produces **actionable discrepancies** between what was planned and what was implemented.

**CRITICAL**: After receiving the subagent's review, create a todo list for interactive review. Present each discrepancy one at a time, STOPPING after each one for the user to decide whether to align, defer, or accept the deviation.

## Review Scope
- Compare the plan document's specifications against the actual implementation
- Identify missing planned features, unauthorized additions, and specification mismatches
- Focus on structural alignment, not code quality (that's covered by code_review.md)
- Provide concrete evidence of alignment or misalignment with file references
- **CRITICAL**: Do NOT include or evaluate any timing, schedules, or timelines from the plan

## Required Output Format

The subagent must return findings in this **exact format** for easy todo list creation:

### Alignment Summary
Brief overview of alignment status (2-3 sentences max)

**Metrics:**
- ✅ Fully aligned items: [COUNT]
- ⚠️ Partially aligned items: [COUNT]  
- ❌ Misaligned items: [COUNT]
- ➕ Unplanned additions: [COUNT]
- ➖ Missing implementations: [COUNT]

**Well-Aligned Areas (Following Plan Correctly):**
- [Brief bullet point about each area that matches the plan well]
- [e.g., "Error handling implemented exactly as specified in Section 3.2"]
- [e.g., "API signatures match plan document perfectly"]

### Alignment Discrepancies

**MISSING-[ID]**: [Feature/component from plan not implemented]
- **Plan Section**: [Section number and title from plan document]
- **Planned Specification**: [What the plan said should exist]
- **Current State**: [What actually exists (or "Not implemented")]
- **Files Expected**: [Where this should have been implemented]
- **Priority**: [High/Medium/Low based on plan emphasis]
- **Impact**: [Consequences of this missing implementation]

**MISMATCH-[ID]**: [Implementation differs from plan specification]
- **Plan Section**: [Section number and title from plan document]
- **Planned Approach**: [How the plan specified to implement]
- **Actual Implementation**: [How it was actually implemented]
- **Location**: [File path and line numbers of actual implementation]
- **Priority**: [High/Medium/Low]
- **Deviation Rationale**: [Possible reason for deviation if apparent]

**PARTIAL-[ID]**: [Incomplete implementation of planned feature]
- **Plan Section**: [Section number and title from plan document]
- **Planned Scope**: [Full scope from plan]
- **Implemented Portion**: [What was actually completed]
- **Missing Portions**: [What remains unimplemented]
- **Location**: [File path of partial implementation]
- **Priority**: [High/Medium/Low]
- **Complexity**: [Simple/Moderate/Complex to complete]

**UNPLANNED-[ID]**: [Implementation not specified in plan]
- **Location**: [File path and line numbers]
- **Addition Description**: [What was added beyond plan]
- **Category**: [Enhancement/Refactor/Helper/Debug]
- **Priority**: [High/Medium/Low for review]
- **Value Assessment**: [Does this improve or complicate the design?]
- **Recommendation**: [Keep/Remove/Document in plan]

**SPECIFICATION-[ID]**: [Technical specification violation]
- **Plan Section**: [Section with technical spec]
- **Specified Requirement**: [Exact requirement from plan]
- **Actual Behavior**: [How implementation behaves]
- **Location**: [File path and line numbers]
- **Priority**: [High/Medium/Low]
- **Fix Complexity**: [Simple/Moderate/Complex to align]

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
```
Task a general-purpose subagent to review implementation alignment with [PLAN DOCUMENT NAME] and provide actionable discrepancies in the exact format specified above.

**MANDATORY FIRST STEP - READ PLAN DOCUMENT**:
1. Read the complete plan document: `$ARGUMENTS`
2. Extract and list ALL planned features, specifications, and requirements
3. Create a mental checklist of what should exist according to the plan
4. Note any sections marked as "optional" or "future work"

**MANDATORY SECOND STEP - MAP IMPLEMENTATION**:
1. Search the codebase for each planned feature
2. Read the actual implementation files
3. Compare actual code against plan specifications
4. Look for code that wasn't mentioned in the plan

**CRITICAL THIRD STEP - BUILD WARNING ANALYSIS**:
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

**CRITICAL FOURTH STEP - ALIGNMENT ANALYSIS**:
For each planned item, determine:
- **Fully Aligned**: Implementation matches plan exactly
- **Partially Aligned**: Some aspects match, others don't (including unused code for planned features)
- **Misaligned**: Implementation contradicts plan
- **Missing**: Plan item not implemented at all
- **Unplanned**: Implementation has no plan reference (including dead code to remove)

**EVIDENCE REQUIREMENTS**:
Every discrepancy must include:
1. Exact quote or section reference from plan document
2. Actual code snippet or confirmation of absence
3. Specific file paths and line numbers
4. Clear explanation of the discrepancy

**IMPORTANT FILTERS**:
- Ignore code quality issues (covered by code_review.md)
- Ignore plan quality issues (covered by design_review.md)
- Focus ONLY on plan-to-implementation alignment
- Don't flag intentional plan sections marked "future work" or "optional"
- Don't flag standard boilerplate (tests, imports, etc.) as unplanned
- IGNORE any timing, schedule, or timeline information in the plan - focus only on technical specifications

Return results in the structured format with MISSING-*, MISMATCH-*, PARTIAL-*, UNPLANNED-*, and SPECIFICATION-* categories.

Each finding must be actionable - provide enough detail for immediate correction.
Include positive findings in the "Well-Aligned Areas" section to acknowledge good adherence.
```

## Post-Review Instructions - Keyword-Driven Alignment Process

**CRITICAL**: Read `~/.claude/commands/shared/keyword_review_pattern.txt` and follow the shared patterns with these customizations:

### Pattern Application for Plan Alignment Review
- Follow `<CorePresentationFlow>` with [Review Type] = "Plan Alignment Review" and [items] = "discrepancies"
- Follow `<KeywordDecisionProcess>` with keywords: align to plan, skip, accept as built, investigate
- Follow `<InvestigationPattern>` with [DOMAIN VALUES] = "plan adherence and pragmatism"
- Follow `<CumulativeUpdateRule>` for both code and plan updates
- Follow `<EnforcementRules>` completely
- Use `<FinalSummaryTemplate>` with alignment categories (MISSING, MISMATCH, UNPLANNED, etc.)

### Alignment Review Specific Keywords
**Primary keywords**: align to plan, skip, accept as built, investigate
**After investigation**: Varies by verdict (see investigation verdict handling below)

### Alignment Review Keyword Response Actions

**When user responds "align to plan"**:
1. **IMPLEMENT ALIGNMENT**: Modify the code to match the plan specification
2. Show the implemented change with before/after comparison
3. Mark current todo as completed
4. **CRITICAL**: Before presenting the next discrepancy, review ALL alignments made in this session so far. Update remaining discrepancies to account for the cumulative changes - some mismatches may now be resolved, line numbers may have shifted, and new considerations may apply.
5. Present next discrepancy, then EXPLICITLY state the available keywords:
   ```
   Please respond with one of these keywords:
   - "align to plan" - ACTION: Modify implementation to match the plan specification
   - "skip" - ACTION: Defer alignment to later and document in TECH_DEBT.md
   - "accept as built" - ACTION: Accept deviation and update plan documentation
   - "investigate" - ACTION: Launch deep analysis to understand the discrepancy
   ```
   Then STOP

**When user responds "skip"**:
1. **DOCUMENT IN TECH_DEBT.md**: Add entry for deferred alignment:
   ```markdown
   ### Deferred Alignment: [DISCREPANCY-ID]
   - **Plan Section**: [Reference]
   - **Current State**: [Brief description]
   - **Required Change**: [What needs to be done]
   - **Deferral Date**: [Today's date]
   - **Reason**: User decision - alignment deferred
   ```
2. Mark current todo as completed
3. **CRITICAL**: Before presenting the next discrepancy, review ALL changes (alignments, deferrals, acceptances) made in this session so far. Adjust remaining discrepancies based on the cumulative state.
4. Present next discrepancy, then EXPLICITLY state the available keywords:
   ```
   Please respond with one of these keywords:
   - "align to plan" - ACTION: Modify implementation to match the plan specification
   - "skip" - ACTION: Defer alignment to later and document in TECH_DEBT.md
   - "accept as built" - ACTION: Accept deviation and update plan documentation
   - "investigate" - ACTION: Launch deep analysis to understand the discrepancy
   ```
   Then STOP

**When user responds "accept as built"**:
1. **UPDATE PLAN DOCUMENT**: Add deviation note to the plan:
   ```markdown
   ### Implementation Note: [DISCREPANCY-ID]
   **Deviation from Original Plan**
   - **Original**: [What was planned]
   - **Actual**: [What was implemented]
   - **Rationale**: Accepted deviation - [reason if provided]
   - **Date**: [Today's date]
   ```
2. Mark current todo as completed
3. **CRITICAL**: Before presenting the next discrepancy, review ALL changes (alignments, deferrals, acceptances) made in this session so far. Adjust remaining discrepancies based on the cumulative state.
4. Present next discrepancy, then EXPLICITLY state the available keywords:
   ```
   Please respond with one of these keywords:
   - "align to plan" - ACTION: Modify implementation to match the plan specification
   - "skip" - ACTION: Defer alignment to later and document in TECH_DEBT.md
   - "accept as built" - ACTION: Accept deviation and update plan documentation
   - "investigate" - ACTION: Launch deep analysis to understand the discrepancy
   ```
   Then STOP

**When user responds "investigate"**:
1. **TASK INVESTIGATION AGENT**: Use Task tool to deep-dive with a general-purpose agent acting as a **responsible judge balancing plan adherence and pragmatism**:
   - **CRITICAL JUDGMENT MANDATE**: Act as a responsible engineering judge who values both plan adherence AND practical implementation realities
   - **Understand why the deviation occurred**: Research the technical or practical reasons
   - **Analyze benefits/drawbacks of both approaches**: Weigh plan approach vs actual implementation
   - **Check if plan was unrealistic or implementation is better**: Sometimes reality improves on the plan
   - **Research if technical constraints forced the change**: Understand if deviation was necessary
   - **Provide evidence-based recommendation**: Ground your judgment in concrete technical facts
2. **PRESENT INVESTIGATION FINDINGS**: Return with:
   - **Root Cause**: Why the discrepancy exists
   - **Trade-offs**: Plan approach vs actual approach with clear pros/cons
   - **Technical Analysis**: Constraints and considerations discovered
   - **Investigation Verdict**: One of:
     - **ALIGN RECOMMENDED**: Investigation supports aligning to the plan
     - **ACCEPT RECOMMENDED**: Investigation supports accepting the as-built deviation
     - **DEFER RECOMMENDED**: Investigation suggests deferring due to complexity/risk
3. **WAIT FOR NEW KEYWORD WITH CONTEXT-APPROPRIATE MEANING**: Present findings and then EXPLICITLY state based on the investigation verdict:
   
   **If ALIGN RECOMMENDED**:
   ```
   Investigation verdict: ALIGN RECOMMENDED - Investigation supports aligning implementation to plan
   
   Please respond with one of these keywords:
   - "align to plan" - ACTION: Implement the alignment to match plan specification
   - "skip" - ACTION: Defer alignment despite recommendation
   - "accept as built" - ACTION: Accept deviation despite recommendation
   ```
   
   **If ACCEPT RECOMMENDED**:
   ```
   Investigation verdict: ACCEPT RECOMMENDED - Investigation supports accepting the as-built deviation
   Reason: [specific reason from investigation]
   
   Please respond with one of these keywords:
   - "accept as built" - ACTION: Accept the deviation and update plan documentation
   - "align to plan" - ACTION: Override recommendation and align to plan anyway
   - "skip" - ACTION: Defer decision to later
   ```
   
   **If DEFER RECOMMENDED**:
   ```
   Investigation verdict: DEFER RECOMMENDED - Investigation suggests deferring this alignment
   Reason: [complexity/risk reason from investigation]
   
   Please respond with one of these keywords:
   - "skip" - ACTION: Accept recommendation to defer and document in TECH_DEBT.md
   - "align to plan" - ACTION: Override recommendation and align now
   - "accept as built" - ACTION: Accept deviation instead of deferring
   ```

### Final Summary
When all discrepancies are addressed, provide:
```
Plan Alignment Review Complete!

**Results Summary:**
- Discrepancies Aligned: [X]
  - MISSING: [count] items now implemented
  - MISMATCH: [count] items corrected
  - SPECIFICATION: [count] items fixed
  
- Discrepancies Deferred: [Y]
  - [List with brief descriptions]
  - All documented in TECH_DEBT.md
  
- Deviations Accepted: [Z]
  - [List with brief descriptions]
  - Plan document updated with notes
  
- Unplanned Additions: [W]
  - Removed: [count]
  - Kept & Documented: [count]

- Well-Aligned Areas Maintained: [V]
  - [Quick recap of areas that matched plan well]

Plan and implementation are now synchronized.
```

## Relationship to Other Commands

- **After `task_a_subagent.md`**: Use this to verify implementation matches plan
- **Before `code_review.md`**: Ensure structural alignment before quality review
- **Complements `design_review.md`**: That reviews the plan, this reviews plan adherence
- **Updates `TECH_DEBT.md`**: Deferred alignments are tracked as technical debt