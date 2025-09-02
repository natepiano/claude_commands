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

### Type System Architecture (MANDATORY FIRST ANALYSIS)
1. **Conditional Audit**: Flag string-based conditionals that could be replaced with enums (NOT string accessors on typed data)
2. **Error Handling**: Check for proper Result/Option usage vs bare `.unwrap()` (NOT `unwrap_or` or `unwrap_or_else`)
3. **String Typing**: Identify stringly-typed APIs that need proper types (NOT strings already in type-safe wrappers)
4. **State Machines**: Find boolean flags that should be state enums
5. **Method vs Function**: Flag utility functions that should be methods

### Standard Analysis
1. **Code Quality**: Naming, formatting, idioms, best practices
2. **Complexity**: Nested conditionals, long functions, unclear logic
3. **Duplication**: Copy-pasted code, similar patterns
4. **Error Handling**: Missing contexts, unclear error messages
5. **Documentation**: Missing or incorrect comments for complex logic

## Prompt Template
```
Task a general-purpose subagent to review [REVIEW TARGET: git diff or specific files] and provide actionable code review feedback in the exact format specified above.

**TARGET SELECTION**:
- If `$ARGUMENTS` is provided: Review all code in specified files/directories (as-built review)
- If no arguments: Review all uncommitted changes via `git diff`
- Use Read tool to examine all files when specific paths are provided

**FIRST STEP - Get the code**:
- If no arguments: Run `git diff` to get uncommitted changes
- If arguments provided: Use Read tool to examine all code in specified files/directories

**CRITICAL SECOND STEP - TYPE SYSTEM VIOLATIONS**:
Before ANY other analysis, audit the code for type system misuse:
- Every if-else chain checking strings should be an enum with pattern matching
- Every utility function should be questioned - why isn't this a method?
- Every boolean flag tracking state should be part of a state enum
- Every stringly-typed parameter should use proper types
- ONLY bare .unwrap() calls should be flagged - unwrap_or() and unwrap_or_else() are acceptable

TREAT STRING-BASED CONDITIONALS AS CODE SMELL: Flag string equality checks against known constants that could be enums. Do NOT flag:
- String accessor methods on typed data (e.g., enum.name())  
- String data already contained in type-safe structures
- Format validation patterns (email, URL parsing, etc.)
- Arbitrary text processing where enums don't make sense

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

## Post-Review Instructions - Keyword-Driven Fix Process

**CRITICAL**: After the subagent returns the review, use ONLY these three keywords for user decisions:

### Initial Review Summary (MANDATORY)
After receiving the subagent's review:

1. **Present Summary Statistics**:
   ```
   Code Review Summary:
   - Total issues found: [X]
   - Positive findings: [Y]
   - Issues to review: [Z]
   ```

2. **Present Issues Overview Table**:
   ```
   | ID | Priority | Category | Brief Description |
   |----|----------|----------|------------------|
   | TYPE-SYSTEM-1 | High | Type System | Replace string conditionals with enum |
   | QUALITY-1 | Medium | Quality | Improve error message context |
   | COMPLEXITY-1 | Low | Complexity | Simplify nested conditionals |
   ```

3. **Acknowledge Positive Findings**:
   List the positive findings as bullet points to acknowledge good practices

4. **Transition Statement**:
   "Let's review each issue. I'll present them one at a time for your decision."

### Keyword Decision Process
1. Create a todo list using TodoWrite with ONLY the actionable issues
2. Present the FIRST issue with full details and STOP
3. **MANDATORY**: Wait for user to respond with EXACTLY one of these keywords:
   - **"fix"** - Implement the fix immediately
   - **"skip"** - Skip this issue
   - **"investigate"** - Deep dive analysis before deciding

### Keyword Response Actions

**When user responds "fix"**:
1. **IMPLEMENT IMMEDIATELY**: Apply the suggested code change
2. **RUN BUILD AND FORMAT**: Execute `cargo build && cargo +nightly fmt` to ensure the fix works
3. Show the implemented change with a brief confirmation
4. Mark current todo as completed
5. **CRITICAL**: Before presenting the next issue, review ALL fixes applied in this session so far. Update remaining issues to account for the cumulative changes - adjust line numbers, remove issues already resolved by previous fixes, and modify suggestions to align with the current state of the code.
6. Present next issue and STOP

**When user responds "skip"**:
1. Mark current todo as completed
2. Add to skipped issues list (in memory for final summary)
3. **CRITICAL**: Before presenting the next issue, review ALL fixes applied and issues skipped in this session so far. Ensure remaining issues are still relevant given the accumulated changes.
4. Present next issue and STOP

**When user responds "investigate"**:
1. **TASK INVESTIGATION AGENT**: Use Task tool to deep-dive with a general-purpose agent:
   - Act as a responsible judge balancing code quality and pragmatism
   - Analyze whether the fix provides genuine value vs over-complication
   - Research alternative approaches and trade-offs
   - Examine real-world impact and complexity costs
   - Check if simpler solutions exist
   - Provide evidence-based assessment
2. **PRESENT INVESTIGATION FINDINGS**:
   - **Value Assessment**: Is this worth fixing?
   - **Alternative Approaches**: 2-3 options if applicable
   - **Complexity Analysis**: Implementation cost vs benefit
   - **Recommendation**: Updated suggestion based on investigation
3. **WAIT FOR NEW KEYWORD**: Present findings and wait for "fix" or "skip" (no longer "investigate")

### Keyword Enforcement Rules
- **NO OTHER RESPONSES ACCEPTED**: Only "fix", "skip", or "investigate" trigger actions
- **MANDATORY STOPPING**: ALWAYS stop after each issue presentation
- **NO ASSUMPTIONS**: Never assume user intent - wait for explicit keyword
- **NO BATCHING**: Process exactly one issue per user keyword response
- **INVESTIGATE LIMITATION**: "investigate" only available on first presentation - after investigation, only "fix" or "skip" accepted

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