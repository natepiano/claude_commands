# Code Review

**CRITICAL** before doing anything else, read the contents of ~/.claude/commands/shared/review_commands.md and use the tagged sections wherever they are referenced.

<ExecutionSteps/>

<ReviewContext>
[REVIEW_TARGET]: the code changes in the current git diff
[REVIEW_CONTEXT]: We are reviewing ACTUAL CODE for quality issues, NOT a plan. We're looking at real implementation code to find bugs, quality issues, and improvements IN THE CODE.
</ReviewContext>

<ReviewCategories>
- **TYPE-SYSTEM**: Type system violations - missed opportunities for better type safety
- **QUALITY**: Code quality issues - readability, maintainability, and best practice violations
- **COMPLEXITY**: Unnecessary complexity - code that can be simplified or refactored
- **DUPLICATION**: Code duplication - repeated logic that should be extracted
- **SAFETY**: Safety concerns - error handling and potential runtime issues
</ReviewCategories>

<ReviewConstraints>
    - <TypeSystemPrinciples/>
</ReviewConstraints>

<ReviewKeywords>
    **For FIX RECOMMENDED verdicts:**
    - **fix**: Apply the suggested code change immediately
    - **skip**: Skip this fix and continue
    - **investigate**: Launch deeper investigation of the code issue

    **For FIX MODIFIED verdicts:**
    - **fix**: Apply the modified code change
    - **skip**: Skip this fix and continue
    - **investigate**: Launch deeper investigation

    **For FIX NOT RECOMMENDED verdicts:**
    - **accept**: Accept the recommendation to not fix (default)
    - **override**: Apply the fix despite the recommendation
    - **investigate**: Launch investigation to reconsider
</ReviewKeywords>

<ReviewFollowupParameters>
    When using ReviewFollowup from review_commands.md, substitute:
    - [EXPECTED_VERDICTS]: FIX RECOMMENDED, FIX MODIFIED, or FIX NOT RECOMMENDED
</ReviewFollowupParameters>

<KeywordExecution>
    **fix**: Use Edit tool to apply the suggested_code changes to the files specified in location
    **skip**: Mark as skipped and continue (maintain list for final summary)
    **accept**: Mark as accepted (agreeing with FIX NOT RECOMMENDED verdict) and continue
    **override**: Use Edit tool to apply the suggested_code despite FIX NOT RECOMMENDED verdict
    **investigate**: Ask user "What specific aspect would you like me to investigate?", then launch Task tool with their focus
</KeywordExecution>
