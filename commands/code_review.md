# Code Review

**MANDATORY FIRST STEP**: 
1. Use the Read tool to read /Users/natemccoy/.claude/commands/shared/review_commands.md
2. Find and follow the <ExecutionSteps> section from that file
3. When you see tags like <ExecutionSteps/> below, these refer to sections in review_commands.md

<ExecutionSteps/>

<InitialReviewOutput>
Step 1: Initial Code Review

  Review Target: [REVIEW_TARGET]

  Now I'll launch the Task tool for the initial review:
</InitialReviewOutput>

<DetermineReviewTarget>
**Execute this step to determine what to review:**

If $ARGUMENTS starts with "tag:":
- Extract the tag name by removing the "tag:" prefix
- Set [REVIEW_TARGET] to: all code changes since git tag [TAG_NAME] to current working state
- Set [REVIEW_MODE] to: tag review (reviewing all changes since a tagged release)
- Execute: git diff [TAG_NAME] HEAD --name-only to get all affected files
- Execute: git diff [TAG_NAME] HEAD to get all changes since the tag
- Note: This includes both committed and uncommitted changes since the tag

If $ARGUMENTS starts with "commit:":
- Extract the commit hash by removing the "commit:" prefix
- Set [REVIEW_TARGET] to: the code changes from git commit [COMMIT_HASH]
- Set [REVIEW_MODE] to: commit review (reviewing changes from a specific commit)
- Execute: git show [COMMIT_HASH] --name-only to get affected files
- Execute: git show [COMMIT_HASH] to get the changes

If $ARGUMENTS is provided and does NOT start with "commit:" or "tag:":
- Set [REVIEW_TARGET] to: the code at path $ARGUMENTS (and all files below it if it's a directory)
- Set [REVIEW_MODE] to: static code review (reviewing actual code as-is, not changes)
- Use glob/grep tools to find all code files under $ARGUMENTS path

If $ARGUMENTS is empty:
- Set [REVIEW_TARGET] to: the code changes from running: git diff -- . ':(exclude,top)*.md'
- Set [REVIEW_MODE] to: diff review (reviewing uncommitted changes only)
- Execute git diff to get the changes

Set [REVIEW_CONTEXT] to: We are reviewing ACTUAL CODE for quality issues, NOT a plan. We're looking at real implementation code to find bugs, quality issues, and improvements IN THE CODE.
</DetermineReviewTarget>

<ReviewCategories>
- **TYPE-SYSTEM**: Type system violations - missed opportunities for better type safety
- **QUALITY**: Code quality issues - readability, maintainability, and best practice violations
- **COMPLEXITY**: Unnecessary complexity - code that can be simplified or refactored
- **DUPLICATION**: Code duplication - repeated logic that should be extracted
- **SAFETY**: Safety concerns - error handling and potential runtime issues
</ReviewCategories>

<ReviewConstraints>
    - <TypeSystemPrinciples/>
    - <CodeDuplicationDetection/>
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
