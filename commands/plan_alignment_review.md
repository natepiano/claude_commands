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

### Deviation Classification
1. **Justified Deviations**: Implementation improvements discovered during coding
2. **Unjustified Deviations**: Arbitrary changes without clear benefit
3. **Technical Necessities**: Changes required by unforeseen constraints
4. **Scope Creep**: Additions beyond planned requirements
5. **Incomplete Work**: Planned items not yet implemented

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

**CRITICAL THIRD STEP - ALIGNMENT ANALYSIS**:
For each planned item, determine:
- **Fully Aligned**: Implementation matches plan exactly
- **Partially Aligned**: Some aspects match, others don't
- **Misaligned**: Implementation contradicts plan
- **Missing**: Plan item not implemented at all
- **Unplanned**: Implementation has no plan reference

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

**CRITICAL**: After the subagent returns the review, use ONLY these four keywords for user decisions:

### Initial Review Summary (MANDATORY)
After receiving the subagent's review:

1. **Present Summary Statistics**:
   ```
   Plan Alignment Review Summary:
   - Total discrepancies found: [X]
   - Well-aligned areas: [Y]
   - Discrepancies to review: [Z]
   ```

2. **Present Discrepancy Overview Table**:
   ```
   | ID | Priority | Category | Brief Description |
   |----|----------|----------|------------------|
   | MISSING-1 | High | Missing | Error recovery system not implemented |
   | MISMATCH-1 | Medium | Mismatch | Used HashMap instead of planned BTreeMap |
   | UNPLANNED-1 | Low | Unplanned | Added debug logging system |
   ```

3. **Acknowledge Well-Aligned Areas**:
   List the well-aligned findings as bullet points to acknowledge good plan adherence

4. **Transition Statement**:
   "Let's review each discrepancy. I'll present them one at a time for your decision."

### Keyword Decision Process
1. Create a todo list using TodoWrite with ONLY the discrepancies
2. Present the FIRST discrepancy with full details and STOP
3. **MANDATORY**: Wait for user to respond with EXACTLY one of these keywords:
   - **"align to plan"** - Modify implementation to match the plan
   - **"skip"** - Acknowledge but defer alignment to later
   - **"accept as built"** - Accept the deviation and update plan documentation
   - **"investigate"** - Deep dive to understand the discrepancy better

### Keyword Response Actions

**When user responds "align to plan"**:
1. **IMPLEMENT ALIGNMENT**: Modify the code to match the plan specification
2. Show the implemented change with before/after comparison
3. Mark current todo as completed
4. **CRITICAL**: Before presenting the next discrepancy, review ALL alignments made in this session so far. Update remaining discrepancies to account for the cumulative changes - some mismatches may now be resolved, line numbers may have shifted, and new considerations may apply.
5. Present next discrepancy and STOP

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
4. Present next discrepancy and STOP

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
4. Present next discrepancy and STOP

**When user responds "investigate"**:
1. **TASK INVESTIGATION AGENT**: Use Task tool to deep-dive with a general-purpose agent:
   - Understand why the deviation occurred
   - Analyze benefits/drawbacks of both approaches
   - Check if plan was unrealistic or implementation is better
   - Research if technical constraints forced the change
   - Provide evidence-based recommendation
2. **PRESENT INVESTIGATION FINDINGS**:
   - **Root Cause**: Why the discrepancy exists
   - **Trade-offs**: Plan approach vs actual approach
   - **Technical Analysis**: Constraints and considerations
   - **Recommendation**: Align, defer, or accept with rationale
3. **WAIT FOR NEW KEYWORD**: Present findings and wait for "align to plan", "skip", or "accept as built" (no longer "investigate")

### Keyword Enforcement Rules
- **NO OTHER RESPONSES ACCEPTED**: Only "align to plan", "skip", "accept as built", or "investigate" trigger actions
- **MANDATORY STOPPING**: ALWAYS stop after each discrepancy presentation
- **NO ASSUMPTIONS**: Never assume user intent - wait for explicit keyword
- **NO BATCHING**: Process exactly one discrepancy per user keyword response
- **INVESTIGATE LIMITATION**: "investigate" only available on first presentation

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