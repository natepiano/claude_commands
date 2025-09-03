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

**DESIGN-[ID]**: [Brief title of plan improvement]
- **Plan Issue**: [What design aspect the PLAN doesn't address adequately]
- **Recommendation**: [How to improve THE PLAN - specify section to add/edit]
- **Plan Section**: [Exact section name/number in the plan document]
- **Priority**: [High/Medium/Low]
- **Rationale**: [Why this plan improvement matters]
- **Plan Improvement Proposal**:
  ```markdown
  # Current Plan Text:
  [Quote what the plan currently says about this]
  
  # Proposed Plan Text:
  [Show the improved plan section/addition]
  ```
  ```rust
  // Supporting Code Context (if relevant):
  [Show existing code that helps explain why this plan change is needed]
  ```

**IMPLEMENTATION-[ID]**: [Brief title of missing plan detail]
- **Plan Gap**: [What implementation detail THE PLAN is missing]
- **Recommendation**: [What to add to THE PLAN about implementation]
- **Plan Section**: [Where in the plan this detail should go]
- **Priority**: [High/Medium/Low]
- **Dependencies**: [What plan sections this relates to]
- **Plan Addition Proposal**:
  ```markdown
  # Missing from Plan:
  [Describe what implementation detail is not covered]
  
  # Proposed Plan Addition:
  [Show the implementation details to add to the plan]
  ```
  ```rust
  // Code Context:
  [Show existing code that the plan should address]
  ```

**SIMPLIFICATION-[ID]**: [Brief title of plan simplification]
- **Over-Engineering in Plan**: [What THE PLAN over-complicates]
- **Recommendation**: [How to simplify THE PLAN's approach]
- **Plan Section**: [Which section of the plan is over-engineered]
- **Priority**: [High/Medium/Low]
- **Benefits**: [What this plan simplification achieves]
- **Simplification Proposal**:
  ```markdown
  # Current Plan Approach:
  [Quote the complex approach from the plan]
  
  # Simplified Plan Approach:
  [Show the simplified version for the plan]
  ```

**TYPE-SYSTEM-[ID]**: [Brief title - e.g., "Plan should specify enum for X"]
- **Type System Gap**: [Where THE PLAN misses type-driven design opportunity]
- **Current Code Pattern**: [Existing code that needs better type design]
- **Plan's Current Approach**: [What the plan says about this (or notes if missing)]
- **Proposed Plan Enhancement**: [How the plan should specify type-driven solution]
- **Priority**: [High - these are ALWAYS high priority]
- **Type Design Proposal for Plan**:
  ```markdown
  # Plan Should Specify:
  [Describe the type-driven approach the plan should mandate]
  ```
  ```rust
  // The plan should include this type design:
  [Show the enum/trait/struct the plan should specify]
  ```

## Analysis Requirements

**CRITICAL**: Read and follow `~/.claude/commands/shared/keyword_review_pattern.txt` section `<TypeSystemDesignPrinciples>` for comprehensive analysis guidance.

### Design Review Specific Analysis
Beyond the shared principles, also analyze:
1. **Gap Analysis**: Missing components, unclear specifications
2. **Breaking Changes**: API changes, signature modifications, structural changes
3. **Over-engineering**: Unnecessary complexity that can be simplified
4. **Implementation Order**: Dependencies and sequencing issues (NO time estimates)
5. **Code Examples**: Only when essential for clarity (prefer brief descriptions)
6. **NO TIMING**: Never include schedules, timelines, deadlines, or time estimates

## Prompt Template
```
Task a general-purpose subagent to review [REVIEW TARGET: plan markdown document (default), specific files, or focus area] to evaluate THE PLAN'S DESIGN QUALITY and provide recommendations for IMPROVING THE PLAN ITSELF.

**CRITICAL CONTEXT**: You are reviewing a PLAN DOCUMENT to improve its design, NOT checking if it has been implemented.
- The plan describes FUTURE work to be done
- Your job is to identify gaps, over-engineering, or improvements IN THE PLAN
- You should read current code to understand context, but you're improving THE PLAN, not the code

**TARGET SELECTION**: 
- If `$ARGUMENTS` is provided, review the specified `plan*.md` file in the project root
- If no arguments are provided, review the plan markdown document currently being worked on
- Include specific section names where edits should be made in the target document

**MANDATORY FIRST STEP - CHECK SKIP NOTES**:
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

**MANDATORY SECOND STEP - THOROUGH DOCUMENT READING**:
<DocumentComprehension>
1. **READ THE ENTIRE PLAN** from beginning to end before making any recommendations
2. **SEARCH FOR EXISTING SOLUTIONS** - before claiming something is missing, search the document for related content
3. **QUOTE SPECIFIC SECTIONS** - when claiming gaps exist, quote the relevant plan sections and explain exactly what's missing
4. **CROSS-REFERENCE SECTIONS** - many topics span multiple sections, check all related areas
5. **VERIFICATION REQUIREMENT**: For every "missing" claim, you MUST either:
   - Quote the plan section that should contain it but doesn't, OR
   - Explain why the existing content is insufficient with specific examples
</DocumentComprehension>

**MANDATORY THIRD STEP - UNDERSTAND THE REVIEW SCOPE**:
<ReviewScope>
1. Carefully read and understand what you're reviewing (plan, files, or specific focus area)
2. Note what is already present, implemented, or explicitly planned in the review scope
3. DO NOT suggest things that are already part of what you're reviewing
4. DO NOT suggest anything that appears in the Skip Notes section
5. Focus on genuine gaps, improvements, and issues not already addressed
</ReviewScope>

**CRITICAL FOURTH STEP - TYPE SYSTEM VIOLATIONS**:
Follow `<TypeSystemDesignPrinciples>` from the shared pattern file exactly. This includes all primary type system violations, error handling standards, and analysis priority order.

Then proceed with standard analysis:
1. Identifying ACTUAL gaps and missing components (not things already present in the review scope)
2. Finding breaking changes not explicitly called out
3. Spotting over-engineering and unnecessary complexity
4. Ensuring implementation order makes sense
5. Creating concrete, discussable recommendations for improvements NOT already addressed

**MANDATORY FINAL FILTER - DUPLICATE PREVENTION**:
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

**ENFORCEMENT**: Any recommendation that's a variation of a Skip Notes item will be immediately rejected and may result in prejudice warnings.

Return results in the structured format with TYPE-SYSTEM-*, DESIGN-*, IMPLEMENTATION-*, and SIMPLIFICATION-* categories. TYPE-SYSTEM recommendations should come FIRST and be treated as highest priority.

**CRITICAL REMINDER**: Do NOT include any timing information in recommendations:
- NO schedules, deadlines, or milestones
- NO time estimates or durations
- NO timeline or project phases with dates
- Focus ONLY on technical design and implementation structure

**CRITICAL - IMPLEMENTATION PROPOSALS REQUIRED**: For each recommendation to improve THE PLAN, you MUST include concrete examples:
- **Current Plan**: Show what the plan currently says about this area (quote from the plan document)
- **Current Code**: Show the actual existing code that the plan is trying to improve (read files to get real code)  
- **Proposed Plan Change**: Show how THE PLAN should be modified to address your recommendation
- Use proper syntax highlighting with ```rust code blocks for code examples
- Use markdown quotes for plan text
- Your recommendations are for improving THE PLAN DOCUMENT, not for implementing code

For each recommendation, specify the exact location in the plan document where it should be added or edited (e.g., "Add to Section 3.2 Implementation Details" or "Edit the API Design section").

[INSERT REVIEW TARGET AND ANY CUSTOM INSTRUCTIONS HERE]
```

## Post-Review Instructions - Auto-Investigation and Keyword Process

**STEP 1: RECEIVE SUBAGENT'S REVIEW**
Get the initial design review findings from the subagent.

**STEP 2: EXECUTE PARALLEL INVESTIGATION**
IMMEDIATELY after receiving the subagent's review:
1. Filter out any findings already in Skip Notes or already addressed
2. Launch parallel investigation agents for ALL remaining findings using multiple Task tool invocations in a single response
3. **CRITICAL**: Pass the COMPLETE finding to each investigation agent, including the Current and Proposed code examples
4. **CRITICAL**: Instruct each investigation agent with these EXACT words:
   "You MUST use the MANDATORY Investigation Output Template from <InvestigationAgentInstructions>. Copy the exact template structure and fill it out completely. Include Current State and Proposed Change sections with code blocks. Any response not following this template is INVALID."
   
   **Investigation specifics**:
   - **DOMAIN VALUES**: "code elegance AND practical utility"
   - **INVESTIGATION FOCUS**:
     - Evaluate architectural merit and system design improvements
     - Assess type system usage to eliminate bugs and express intent
     - Consider API elegance and interface design quality
     - Analyze pattern consistency with existing codebase
     - Check for over-engineering vs solving real problems
     - Consider how this affects future evolution and extensions
   - **EXPECTED VERDICTS**: CONFIRMED, MODIFIED, or REJECTED

5. Wait for all investigations to complete
6. **VALIDATION**: Check each investigation result - REJECT any that don't use the template with code blocks
7. Merge compliant findings with their investigation results

**STEP 3: BEGIN KEYWORD-DRIVEN REVIEW**
Now follow the shared patterns from `~/.claude/commands/shared/keyword_review_pattern.txt`:
- Follow `<CorePresentationFlow>` with [Review Type] = "Design Review" and [items] = "recommendations"
- Follow `<ParallelInvestigationPattern>` Step 6 for presenting pre-investigated findings
- Follow `<InvestigateFurtherPattern>` if user requests deeper analysis
- Follow `<KeywordDecisionProcess>` for keyword enforcement
- Follow `<CumulativeUpdateRule>` for plan document updates
- Follow `<EnforcementRules>` completely
- Use `<FinalSummaryTemplate>` with design review categories

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
5. Present next recommendation with its investigation verdict, then EXPLICITLY state the available keywords based on verdict
6. STOP and wait for user response

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
4. Present next recommendation with its investigation verdict, then EXPLICITLY state the available keywords based on verdict
5. STOP and wait for user response

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
4. Present next recommendation with its investigation verdict, then EXPLICITLY state the available keywords based on verdict
5. STOP and wait for user response

**When user responds "investigate further"**:
1. **ASK FOR GUIDANCE**: "What specific aspect would you like me to investigate further?"
2. **WAIT FOR USER INPUT**: Get their specific investigation focus
3. **TASK FOCUSED INVESTIGATION**: Launch new investigation with user's guidance
4. **PRESENT SUPPLEMENTAL FINDINGS**: Show new insights
5. **OFFER SAME KEYWORDS**: Present keywords based on updated verdict (no more "investigate further" to prevent loops)

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

## Default Review Target
**DEFAULT**: When not specified otherwise, the design reviewer should review and edit the plan markdown file currently being worked on. The subagent should be instructed to review this document and provide recommendations for improving it.
