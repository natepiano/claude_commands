# Actionable Code Review Command

## Arguments
- **Default (no arguments)**: Reviews all uncommitted changes via `git diff`
- **With file/directory paths**: Reviews all code in specified files or directories (as-built review)
  - Example: `/code_review src/parser/*.rs` - reviews all code in parser files
  - Example: `/code_review lib/utils.js tests/` - reviews all code in utils.js and all test files

## Overview
Use the Task tool with a general-purpose agent to conduct a comprehensive code review of the specified scope. The review produces **actionable recommendations for immediate fixing**.

**CRITICAL**: After receiving the subagent's review, create a todo list for interactive review. Present each issue one at a time, STOPPING after each one for the user to decide whether to fix, skip, or investigate it.

## Review Scope
- **Default**: Analyze the current git diff (uncommitted changes)
- **With arguments**: Analyze all code in specified files/directories (as-built review)
- Identify code quality issues, complexity, duplication, and type system misuse
- Focus on actionable improvements that can be immediately fixed
- Provide concrete, specific recommendations with code samples

## Required Output Format

The subagent must return issues in this **exact format** for easy todo list creation:

### Review Summary
Brief overview of the changes reviewed (1-2 sentences max)

**Metrics:**
- ✅ Positive findings: [COUNT]
- 🔧 Suggested fixes: [COUNT]

**Positive Findings (Good Practices Observed):**
- [Brief bullet point about each positive aspect found]
- [e.g., "Excellent use of enum pattern matching replacing string conditionals"]
- [e.g., "Proper type-safe design with EnumVariantInfo eliminating invalid states"]
- [e.g., "Good refactoring from if-else chains to pattern matching"]

### Actionable Issues
**IMPORTANT: Only include actual problems that need fixing. Do NOT include positive examples as issues.**

**TYPE-SYSTEM-[ID]**: [Brief title - e.g., "Replace string comparison with enum"]
- **Location**: [File path and line numbers]
- **Issue**: [Type system violation or missed opportunity]
- **Current Code**: [Code snippet showing the issue]
- **Suggested Code**: [Improved code using proper types]
- **Priority**: High (type system issues are always high priority)
- **Impact**: [Why this matters for safety/maintainability]

**QUALITY-[ID]**: [Brief title of issue]
- **Location**: [File path and line numbers]
- **Issue**: [Specific code quality problem]
- **Current Code**: [Problematic code snippet]
- **Suggested Code**: [Improved version]
- **Priority**: [High/Medium/Low]
- **Impact**: [What this improvement achieves]

**COMPLEXITY-[ID]**: [Brief title - e.g., "Simplify nested conditionals"]
- **Location**: [File path and line numbers]
- **Issue**: [Unnecessary complexity description]
- **Current Code**: [Complex code snippet]
- **Suggested Code**: [Simplified version]
- **Priority**: [High/Medium/Low]
- **Rationale**: [Why the simpler approach is better]

**DUPLICATION-[ID]**: [Brief title - e.g., "Extract common build logic"]
- **Location**: [File paths where duplication occurs]
- **Issue**: [Description of duplicated code]
- **Current Pattern**: [Example of the duplication]
- **Suggested Refactor**: [How to eliminate duplication]
- **Priority**: [High/Medium/Low]
- **Benefits**: [Maintenance and consistency improvements]

**SAFETY-[ID]**: [Brief title - e.g., "Add error context"]
- **Location**: [File path and line numbers]
- **Issue**: [Error handling or safety concern]
- **Current Code**: [Code with safety issue]
- **Suggested Code**: [Safer version]
- **Priority**: [High/Medium/Low]
- **Risk**: [What could go wrong without this fix]

## Known Issues (Do NOT Flag These)

### Acceptable Patterns
1. **Safe unwrap variants**: `unwrap_or()` and `unwrap_or_else()` are acceptable as they provide defaults
2. **Only flag bare `.unwrap()`**: Only suggest fixes for bare `.unwrap()` calls without fallbacks
3. **String validation patterns**: Format validation (email, URLs, etc.) and arbitrary text processing is acceptable
4. **String accessors from typed data**: Methods like `.name()` on well-typed enums that extract string fields are acceptable
5. **String data in type-safe wrappers**: Strings already contained within proper type-safe structures don't need further wrapping

## Analysis Requirements

**CRITICAL**: Read and follow `~/.claude/commands/shared/keyword_review_pattern.txt` section `<TypeSystemDesignPrinciples>` for comprehensive analysis guidance.

### Code Review Specific Analysis
Beyond the shared principles, also analyze:
1. **Code Quality**: Naming, formatting, idioms, best practices
2. **Documentation**: Missing or incorrect comments for complex logic
3. **Test Coverage**: Areas that need better test coverage

## Prompt Template
```
Task a general-purpose subagent to review ACTUAL CODE [REVIEW TARGET: git diff or specific files] and provide actionable code review feedback in the exact format specified above.

**CRITICAL CONTEXT**: You are reviewing ACTUAL CODE for quality issues, NOT a plan.
- You're looking at real implementation code
- Your job is to find bugs, quality issues, and improvements IN THE CODE
- You are NOT comparing against a plan or reviewing documentation

**TARGET SELECTION**:
- If `$ARGUMENTS` is provided: Review all code in specified files/directories (as-built review)
- If no arguments: Review all uncommitted changes via `git diff`
- Use Read tool to examine all files when specific paths are provided

**FIRST STEP - Get the code**:
- If no arguments: Run `git diff` to get uncommitted changes
- If arguments provided: Use Read tool to examine all code in specified files/directories

**CRITICAL SECOND STEP - TYPE SYSTEM VIOLATIONS**:
Follow `<TypeSystemDesignPrinciples>` from the shared pattern file exactly. This includes all primary type system violations, error handling standards, and what NOT to flag.

Then proceed with standard analysis:
1. Identifying code quality issues in the code
2. Finding unnecessary complexity that can be simplified
3. Spotting code duplication
4. Checking error handling and safety
5. Creating concrete, fixable recommendations

**IMPORTANT**: 
- For git diff mode: Only review code that appears in the diff
- For as-built mode: Review all code in specified files/directories
- Provide actual code snippets for current and suggested versions
- Make suggestions that can be immediately implemented
- Prioritize issues by their impact on code quality and maintainability

Return results in the structured format with:
1. Review Summary with metrics and positive findings listed as bullet points
2. ONLY actionable issues in TYPE-SYSTEM-*, QUALITY-*, COMPLEXITY-*, DUPLICATION-*, and SAFETY-* categories
3. Do NOT include positive examples as issues - capture them ONLY in the Positive Findings section

TYPE-SYSTEM recommendations should come FIRST and be treated as highest priority.
Each issue must include the exact location, current code, and suggested code to make fixes straightforward.
```

## Post-Review Instructions - Auto-Investigation and Fix Process

**STEP 1: RECEIVE SUBAGENT'S REVIEW**
Get the initial code review findings from the subagent.

**STEP 2: EXECUTE PARALLEL INVESTIGATION**
IMMEDIATELY after receiving the subagent's review:
1. Filter out any issues already fixed by previous changes
2. Launch parallel investigation agents for ALL remaining issues using multiple Task tool invocations in a single response
3. **CRITICAL**: Pass the COMPLETE issue to each investigation agent, including the Current and Proposed code examples
4. **CRITICAL**: Instruct each investigation agent with these EXACT words:
   "You MUST use the MANDATORY Investigation Output Template from <InvestigationAgentInstructions>. Copy the exact template structure and fill it out completely. Include Current State and Proposed Change sections with code blocks. Any response not following this template is INVALID."
   
   **Investigation specifics**:
   - **DOMAIN VALUES**: "code quality AND pragmatism"
   - **INVESTIGATION FOCUS**:
     - Assess actual bug risk vs stylistic preference
     - Evaluate fix complexity and refactoring scope
     - Consider impact on code clarity and readability
     - Check for performance implications
     - Analyze maintenance burden reduction
     - Verify fix won't introduce new issues
     - Consider alignment with team conventions
   - **EXPECTED VERDICTS**: FIX RECOMMENDED, FIX MODIFIED, or FIX NOT RECOMMENDED

5. Wait for all investigations to complete
6. **VALIDATION**: Check each investigation result - REJECT any that don't use the template with code blocks
7. Merge compliant issues with their investigation results

**STEP 3: BEGIN KEYWORD-DRIVEN REVIEW**
Now follow the shared patterns from `~/.claude/commands/shared/keyword_review_pattern.txt`:
- Follow `<CorePresentationFlow>` with [Review Type] = "Code Review" and [items] = "issues"
- Follow `<ParallelInvestigationPattern>` Step 6 for presenting pre-investigated findings
- Follow `<InvestigateFurtherPattern>` if user requests deeper analysis
- Follow `<KeywordDecisionProcess>` for keyword enforcement
- Follow `<CumulativeUpdateRule>` for code changes (line numbers, resolved issues)
- Follow `<EnforcementRules>` completely
- Use `<FinalSummaryTemplate>` with issue categories

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

**When user responds "fix"**:
1. **IMPLEMENT IMMEDIATELY**: Apply the suggested code change
2. **RUN BUILD AND FORMAT**: Execute `cargo build && cargo +nightly fmt` to ensure the fix works
3. Show the implemented change with a brief confirmation
4. Mark current todo as completed
5. **CRITICAL**: Before presenting the next issue, review ALL fixes applied in this session so far. Update remaining issues to account for the cumulative changes - adjust line numbers, remove issues already resolved by previous fixes, and modify suggestions to align with the current state of the code.
6. Present next issue with its investigation verdict, then EXPLICITLY state the available keywords based on verdict
7. STOP and wait for user response

**When user responds "skip"**:
1. Mark current todo as completed
2. Add to skipped issues list (in memory for final summary)
3. **CRITICAL**: Before presenting the next issue, review ALL fixes applied and issues skipped in this session so far. Ensure remaining issues are still relevant given the accumulated changes.
4. Present next issue with its investigation verdict, then EXPLICITLY state the available keywords based on verdict
5. STOP and wait for user response

**When user responds "investigate further"**:
1. **ASK FOR GUIDANCE**: "What specific aspect would you like me to investigate further?"
2. **WAIT FOR USER INPUT**: Get their specific investigation focus
3. **TASK FOCUSED INVESTIGATION**: Launch new investigation with user's guidance
4. **PRESENT SUPPLEMENTAL FINDINGS**: Show new insights
5. **OFFER SAME KEYWORDS**: Present keywords based on updated verdict (no more "investigate further" to prevent loops)

### Final Summary
When all issues are addressed, provide:
```
Code Review Complete!

**Results Summary:**
- Issues Fixed: [X] 
  - TYPE-SYSTEM: [count]
  - QUALITY: [count]
  - COMPLEXITY: [count]
  - SAFETY: [count]
  - DUPLICATION: [count]
  
- Issues Skipped: [Y]
  - [List of skipped issue IDs with brief descriptions]

- Positive Practices Maintained: [Z]
  - [Quick recap of positive findings]

All changes have been applied to your working directory.
```

**Note**: The key difference from design_review.md is that this implements fixes immediately in the codebase rather than updating a plan document, making it a live fixing session.