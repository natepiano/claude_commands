# Actionable Design Review Command

Use the Task tool with a general-purpose agent to conduct a comprehensive design review that produces **actionable recommendations for discussion**.

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

## Analysis Requirements
1. **Gap Analysis**: Missing components, unclear specifications
2. **Breaking Changes**: API changes, signature modifications, structural changes
3. **Over-engineering**: Unnecessary complexity that can be simplified
4. **Implementation Order**: Dependencies and sequencing issues
5. **Code Examples**: Only when essential for clarity (prefer brief descriptions)

## Prompt Template
```
Task a general-purpose subagent to review our implementation plan and provide actionable recommendations in the exact format specified above. Focus on:

1. Identifying specific gaps and missing components
2. Finding breaking changes not explicitly called out
3. Spotting over-engineering and unnecessary complexity
4. Ensuring implementation order makes sense
5. Creating concrete, discussable recommendations

Return results in the structured format with DESIGN-*, IMPLEMENTATION-*, and SIMPLIFICATION-* categories. Each recommendation should be specific enough to become a todo item and actionable enough for immediate discussion and decision-making.

Current plan to review: [path to plan-type-context.md]
```

## Post-Review Instructions

**IMPORTANT**: After the subagent returns the review:
1. Create a todo list using TodoWrite with each recommendation as a separate todo item
2. Present the FIRST recommendation and STOP
3. Wait for user to decide: skip or implement
4. If implement: update the plan immediately, then show next item
5. If skip: mark complete and show next item
6. STOP after each item for user decision
7. Continue this process iteratively through all recommendations