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
Task a general-purpose subagent to review whether IMPLEMENTED CODE matches [PLAN DOCUMENT NAME] and provide actionable discrepancies in the exact format specified above.

**CRITICAL CONTEXT**: You are checking if the ACTUAL IMPLEMENTATION matches what was planned.
- The plan describes what SHOULD have been built
- The code shows what WAS ACTUALLY built
- Your job is to find where they don't match
- You are NOT reviewing the quality of the plan itself

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

## Post-Review Instructions - Auto-Investigation and Alignment Process

**STEP 1: RECEIVE SUBAGENT'S REVIEW**
Get the initial alignment discrepancy findings from the subagent.

**STEP 2: EXECUTE PARALLEL INVESTIGATION**
IMMEDIATELY after receiving the subagent's review:
1. Filter out any discrepancies already resolved by prior alignments
2. Launch parallel investigation agents for ALL remaining discrepancies using multiple Task tool invocations in a single response
3. **CRITICAL**: Pass the COMPLETE discrepancy to each investigation agent, including the Plan vs Actual code examples
4. **CRITICAL**: Instruct each investigation agent with these EXACT words:
   "You MUST use the MANDATORY Investigation Output Template from <InvestigationAgentInstructions>. Copy the exact template structure and fill it out completely. Include Current State and Proposed Change sections with code blocks. Any response not following this template is INVALID."
   
   **Investigation specifics**:
   - **DOMAIN VALUES**: "plan adherence AND pragmatism"
   - **INVESTIGATION FOCUS**:
     - Understand why the implementation diverged from plan
     - Evaluate technical merit of both approaches
     - Check if implementation discovered improvements
     - Assess complexity/risk of alignment effort
     - Consider unforeseen technical constraints
     - Analyze business/functional impact
     - Determine if plan should be updated instead
   - **EXPECTED VERDICTS**: ALIGN RECOMMENDED, ACCEPT RECOMMENDED, or DEFER RECOMMENDED

5. Wait for all investigations to complete
6. **VALIDATION**: Check each investigation result - REJECT any that don't use the template with code blocks
7. Merge compliant discrepancies with their investigation results

**STEP 3: BEGIN KEYWORD-DRIVEN REVIEW**
Now follow the shared patterns from `~/.claude/commands/shared/keyword_review_pattern.txt`:
- Follow `<CorePresentationFlow>` with [Review Type] = "Plan Alignment Review" and [items] = "discrepancies"
- Follow `<ParallelInvestigationPattern>` Step 6 for presenting pre-investigated findings
- Follow `<InvestigateFurtherPattern>` if user requests deeper analysis
- Follow `<KeywordDecisionProcess>` for keyword enforcement
- Follow `<CumulativeUpdateRule>` for both code and plan updates
- Follow `<EnforcementRules>` completely
- Use `<FinalSummaryTemplate>` with alignment categories

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

**When user responds "align to plan"**:
1. **IMPLEMENT ALIGNMENT**: Modify the code to match the plan specification
2. Show the implemented change with before/after comparison
3. Mark current todo as completed
4. **CRITICAL**: Before presenting the next discrepancy, review ALL alignments made in this session so far. Update remaining discrepancies to account for the cumulative changes - some mismatches may now be resolved, line numbers may have shifted, and new considerations may apply.
5. Present next discrepancy with its investigation verdict, then EXPLICITLY state the available keywords based on verdict
6. STOP and wait for user response

**When user responds "skip"**:
1. **DOCUMENT IN TECH_DEBT.md**: Add entry for deferred alignment:
   ```markdown
   ### Deferred Alignment: [DISCREPANCY-ID]
   - **Plan Section**: [Reference]
   - **Current State**: [Brief description]
   - **Required Change**: [What needs to be done]
   - **Status**: Deferred
   - **Reason**: User decision - alignment deferred
   ```
2. Mark current todo as completed
3. **CRITICAL**: Before presenting the next discrepancy, review ALL changes (alignments, deferrals, acceptances) made in this session so far. Adjust remaining discrepancies based on the cumulative state.
4. Present next discrepancy with its investigation verdict, then EXPLICITLY state the available keywords based on verdict
5. STOP and wait for user response

**When user responds "accept as built"**:
1. **UPDATE PLAN DOCUMENT**: Add deviation note to the plan:
   ```markdown
   ### Implementation Note: [DISCREPANCY-ID]
   **Deviation from Original Plan**
   - **Original**: [What was planned]
   - **Actual**: [What was implemented]
   - **Rationale**: Accepted deviation - [reason if provided]
   - **Status**: Accepted
   ```
2. Mark current todo as completed
3. **CRITICAL**: Before presenting the next discrepancy, review ALL changes (alignments, deferrals, acceptances) made in this session so far. Adjust remaining discrepancies based on the cumulative state.
4. Present next discrepancy with its investigation verdict, then EXPLICITLY state the available keywords based on verdict
5. STOP and wait for user response

**When user responds "investigate further"**:
1. **ASK FOR GUIDANCE**: "What specific aspect would you like me to investigate further?"
2. **WAIT FOR USER INPUT**: Get their specific investigation focus  
3. **TASK FOCUSED INVESTIGATION**: Launch new investigation with user's guidance
4. **PRESENT SUPPLEMENTAL FINDINGS**: Show new insights
5. **OFFER SAME KEYWORDS**: Present keywords based on updated verdict (no more "investigate further" to prevent loops)

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