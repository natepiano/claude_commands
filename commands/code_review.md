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
Task a general-purpose subagent to review [REVIEW TARGET: git diff or specific files] and provide actionable code review feedback in the exact format specified above.

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

## Post-Review Instructions - Keyword-Driven Fix Process

**CRITICAL**: Read `~/.claude/commands/shared/keyword_review_pattern.txt` and follow the shared patterns with these customizations:

### Pattern Application for Code Review
- Follow `<CorePresentationFlow>` with [Review Type] = "Code Review" and [items] = "issues"
- Follow `<KeywordDecisionProcess>` with keywords: fix, skip, investigate
- Follow `<InvestigationPattern>` with [DOMAIN VALUES] = "code quality and pragmatism"
- Follow `<CumulativeUpdateRule>` for code changes (line numbers, resolved issues)
- Follow `<EnforcementRules>` completely
- Use `<FinalSummaryTemplate>` with issue categories (TYPE-SYSTEM, QUALITY, COMPLEXITY, etc.)

### Code Review Specific Keywords
**Primary keywords**: fix, skip, investigate
**After investigation**: fix, skip (for RECOMMENDED/MODIFIED), accept, override (for NOT RECOMMENDED)

### Code Review Keyword Response Actions

**When user responds "fix"**:
1. **IMPLEMENT IMMEDIATELY**: Apply the suggested code change
2. **RUN BUILD AND FORMAT**: Execute `cargo build && cargo +nightly fmt` to ensure the fix works
3. Show the implemented change with a brief confirmation
4. Mark current todo as completed
5. **CRITICAL**: Before presenting the next issue, review ALL fixes applied in this session so far. Update remaining issues to account for the cumulative changes - adjust line numbers, remove issues already resolved by previous fixes, and modify suggestions to align with the current state of the code.
6. Present next issue, then EXPLICITLY state the available keywords:
   ```
   Please respond with one of these keywords:
   - "fix" - ACTION: Implement the suggested fix immediately
   - "skip" - ACTION: Skip this issue without fixing
   - "investigate" - ACTION: Launch deep analysis to validate if this is worth fixing
   ```
   Then STOP

**When user responds "skip"**:
1. Mark current todo as completed
2. Add to skipped issues list (in memory for final summary)
3. **CRITICAL**: Before presenting the next issue, review ALL fixes applied and issues skipped in this session so far. Ensure remaining issues are still relevant given the accumulated changes.
4. Present next issue, then EXPLICITLY state the available keywords:
   ```
   Please respond with one of these keywords:
   - "fix" - ACTION: Implement the suggested fix immediately
   - "skip" - ACTION: Skip this issue without fixing
   - "investigate" - ACTION: Launch deep analysis to validate if this is worth fixing
   ```
   Then STOP

**When user responds "investigate"**:
1. **TASK INVESTIGATION AGENT**: Use Task tool to deep-dive with a general-purpose agent acting as a **responsible judge balancing code quality and pragmatism**:
   - **CRITICAL JUDGMENT MANDATE**: Act as a responsible engineering judge who values both code elegance AND practical utility
   - **Balance aesthetics vs pragmatism**: Weigh the beauty of clean code against real-world implementation costs
   - **Provide sound engineering judgment**: Consider maintenance burden, team cognitive load, and actual business value
   - **Analyze whether the fix provides genuine value vs over-complication**: Be skeptical of perfectionism for perfectionism's sake
   - **Research alternative approaches and trade-offs**: Find the sweet spot between ideal and practical
   - **Examine real-world impact and complexity costs**: Consider developer time, debugging difficulty, and onboarding friction
   - **Check if simpler solutions exist that achieve the same goals**: Sometimes "good enough" is better than perfect
   - **Provide evidence-based assessment**: Ground your judgment in concrete examples and real scenarios
2. **PRESENT INVESTIGATION FINDINGS**: Return with:
   - **Value Assessment**: Clear judgment on whether this is worth fixing
   - **Alternative Approaches**: If multiple valid approaches exist, present 2-3 options with trade-offs
   - **Complexity Analysis**: Honest assessment of implementation cost vs benefit
   - **Investigation Verdict**: One of:
     - **FIX RECOMMENDED**: Investigation supports implementing the fix
     - **FIX MODIFIED**: Investigation suggests a modified approach (specify the changes)
     - **FIX NOT RECOMMENDED**: Investigation recommends NOT fixing this issue
3. **WAIT FOR NEW KEYWORD WITH CONTEXT-APPROPRIATE MEANING**: Present findings and then EXPLICITLY state based on the investigation verdict:
   
   **If FIX RECOMMENDED**:
   ```
   Investigation verdict: FIX RECOMMENDED - Investigation supports implementing this fix
   
   Please respond with one of these keywords:
   - "fix" - ACTION: Implement the recommended fix
   - "skip" - ACTION: Skip despite investigation support
   ```
   
   **If FIX MODIFIED**:
   ```
   Investigation verdict: FIX MODIFIED - Investigation suggests implementing with changes: [describe changes]
   
   Please respond with one of these keywords:
   - "fix" - ACTION: Implement the MODIFIED fix
   - "skip" - ACTION: Skip both original and modified versions
   ```
   
   **If FIX NOT RECOMMENDED**:
   ```
   Investigation verdict: FIX NOT RECOMMENDED - Investigation recommends NOT fixing this issue
   Reason: [specific reason from investigation]
   
   Please respond with one of these keywords:
   - "accept" - ACTION: Accept investigation's recommendation to not fix (equivalent to "skip")
   - "override" - ACTION: Ignore investigation and implement fix anyway
   ```

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