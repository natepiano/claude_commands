# Actionable Design Review Command

Use the Task tool with a general-purpose agent to conduct a comprehensive design review that produces **actionable recommendations for discussion**. Be sure to instruct the agent to think hard about the items in the <ReviewScope/> .

**CRITICAL**: After receiving the subagent's review, create a todo list for interactive review. Present each recommendation one at a time, STOPPING after each one for the user to decide whether to skip or implement it.

## Review Scope
- Analyze the current implementation plan for completeness and feasibility
- Identify gaps, missing components, and implementation issues
- Focus on breaking changes, over-engineering, and simplification opportunities
- Provide concrete, specific recommendations that can be turned into todo items

## Required Output Format

The subagent must return recommendations in this **exact format** for easy todo list creation:

### Summary
Brief overview of findings (2-3 sentences max)

### Actionable Recommendations

**DESIGN-[ID]**: [Brief title of recommendation]
- **Issue**: [What specific problem this addresses]
- **Recommendation**: [Specific action to take]
- **Files Affected**: [Exact file paths if applicable]
- **Priority**: [High/Medium/Low]
- **Rationale**: [Why this change improves the design]

**IMPLEMENTATION-[ID]**: [Brief title of recommendation]
- **Issue**: [Implementation gap or complexity issue]
- **Recommendation**: [Specific implementation change]
- **Files Affected**: [Exact file paths]
- **Priority**: [High/Medium/Low]
- **Dependencies**: [What must be done first, if anything]

**SIMPLIFICATION-[ID]**: [Brief title of recommendation]
- **Issue**: [Over-engineered or complex area]
- **Recommendation**: [How to simplify while preserving functionality]
- **Files Affected**: [Exact file paths]
- **Priority**: [High/Medium/Low]
- **Benefits**: [What this simplification achieves]

**TYPE-SYSTEM-[ID]**: [Brief title - e.g., "Replace conditional chain with enum"]
- **Issue**: [Specific conditional or function that violates type system principles]
- **Current Code Pattern**: [Brief description of the problematic pattern]
- **Proposed Type Design**: [Specific enum/trait/struct to introduce]
- **Files Affected**: [Exact file paths]
- **Priority**: [High - these are ALWAYS high priority]
- **Example**: [Small code snippet showing the type-driven approach]

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
4. **Implementation Order**: Dependencies and sequencing issues
5. **Code Examples**: Only when essential for clarity (prefer brief descriptions)

## Prompt Template
```
Task a general-purpose subagent to review [REVIEW TARGET: plan document, specific files, or focus area] and provide actionable recommendations in the exact format specified above.

**MANDATORY FIRST STEP - UNDERSTAND THE REVIEW SCOPE**:
<ReviewScope>
1. Carefully read and understand what you're reviewing (plan, files, or specific focus area)
2. Note what is already present, implemented, or explicitly planned in the review scope
3. DO NOT suggest things that are already part of what you're reviewing
4. Focus on genuine gaps, improvements, and issues not already addressed
</ReviewScope>

**CRITICAL SECOND PASS - TYPE SYSTEM VIOLATIONS**:
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

**IMPORTANT FILTER**: Before including any recommendation, verify it's not already handled in what you're reviewing. Only suggest genuine improvements and additions.

Return results in the structured format with TYPE-SYSTEM-*, DESIGN-*, IMPLEMENTATION-*, and SIMPLIFICATION-* categories. TYPE-SYSTEM recommendations should come FIRST and be treated as highest priority.

[INSERT REVIEW TARGET AND ANY CUSTOM INSTRUCTIONS HERE]
```

## Post-Review Instructions

**IMPORTANT**: After the subagent returns the review:
1. **FILTER STEP**: Review each recommendation and exclude any that are already addressed in the review scope (plan, files, or focus area being reviewed)
2. Create a todo list using TodoWrite with ONLY the filtered recommendations as separate todo items
3. If a recommendation seems redundant with what's already planned/implemented, skip it entirely
4. Present the FIRST non-redundant recommendation and STOP
5. Wait for user to decide: skip or implement
6. If implement: update the plan/code immediately, then show next item
7. If skip: mark complete and show next item
8. STOP after each item for user decision
9. Continue this process iteratively through all recommendations
