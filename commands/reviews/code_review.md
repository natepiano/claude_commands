# Code Review

**MANDATORY FIRST STEP**:
1. Use the Read tool to read ~/.claude/shared/review_commands.md
2. Find and follow the <ExecutionSteps> section from that file
3. When you see tags like <ExecutionSteps/> below, these refer to sections in review_commands.md

<ReviewConfiguration>
MAX_FOLLOWUP_REVIEWS = 7
CONSTRAINTS_FILE = ~/.claude/commands/reviews/constraints/code_review_constraints.md
</ReviewConfiguration>

<ExecutionSteps/>

<ReviewPersona>
You are a senior software architect and code quality expert with extensive experience in:
- Type-driven development and type safety patterns
- Performance optimization and memory management
- API design and maintainability principles
- Code review best practices across multiple languages
- Design patterns and architectural decisions

Your expertise allows you to:
- Identify subtle bugs and race conditions
- Spot opportunities for better abstractions and type modeling
- Recognize performance bottlenecks and inefficient algorithms
- Detect missing validation and error handling
- Evaluate code against SOLID principles and design patterns

Review with the critical eye of someone responsible for maintaining mission-critical production systems.
</ReviewPersona>

<InitialReviewOutput>
**Step 1**: Initial Code Review
**Review Target**: ${REVIEW_TARGET}
**Max Followup Reviews**: ${MAX_FOLLOWUP_REVIEWS}
Now I'll launch the Task tool for the initial codereview:
</InitialReviewOutput>

<DetermineReviewTarget>
**Execute this step to determine what to review:**

**Variables set by this section:**
- REVIEW_TARGET = what code/changes to review (determined from $ARGUMENTS)
- REVIEW_MODE = type of review being performed (tag/commit/static/diff review)
- REVIEW_CONTEXT = explanation of what we're reviewing for the subagent

If $ARGUMENTS starts with "tag:":
- Extract the tag name by removing the "tag:" prefix
- REVIEW_TARGET = all code changes since git tag ${TAG_NAME} to current working state
- REVIEW_MODE = tag review (reviewing all changes since a tagged release)
- Execute: git diff ${TAG_NAME} HEAD --name-only to get all affected files
- Execute: git diff ${TAG_NAME} HEAD to get all changes since the tag
- Note: This includes both committed and uncommitted changes since the tag

If $ARGUMENTS starts with "commit:":
- Extract the commit hash by removing the "commit:" prefix
- REVIEW_TARGET = the code changes from git commit ${COMMIT_HASH}
- REVIEW_MODE = commit review (reviewing changes from a specific commit)
- Execute: git show ${COMMIT_HASH} --name-only to get affected files
- Execute: git show ${COMMIT_HASH} to get the changes

If $ARGUMENTS is provided and does NOT start with "commit:" or "tag:":
- REVIEW_TARGET = the code at path $ARGUMENTS (and all files below it if it's a directory)
- REVIEW_MODE = static code review (reviewing actual code as-is, not changes)
- Use glob/grep tools to find all code files under $ARGUMENTS path

If $ARGUMENTS is empty:
- REVIEW_TARGET = the code changes from running: git diff -- . ':(exclude,top)*.md'
- REVIEW_MODE = diff review (reviewing uncommitted changes only)
- Execute git diff to get the changes

REVIEW_CONTEXT = We are reviewing ACTUAL CODE for quality issues, NOT a plan. We're looking at real implementation code to find bugs, quality issues, and improvements IN THE CODE.
</DetermineReviewTarget>

<ReviewCategories>
- **TYPE-SYSTEM**: Type system violations - missed opportunities for better type safety
- **QUALITY**: Code quality issues - readability, maintainability, and best practice violations
- **COMPLEXITY**: Unnecessary complexity - code that can be simplified or refactored
- **DUPLICATION**: Code duplication - repeated logic that should be extracted
- **SAFETY**: Safety concerns - error handling and potential runtime issues
</ReviewCategories>

## REVIEW CONSTRAINTS

Review constraints are defined in: ${CONSTRAINTS_FILE}

The constraints file is used by both initial review and investigation phases.
The main difference is how each phase handles validation failures:
- **Initial Review**: Discard findings that fail validation
- **Investigation**: Use FIX NOT RECOMMENDED verdict for findings that fail validation

<ReviewKeywords>
    **For FIX RECOMMENDED verdicts:**
    - fix: Apply the suggested code change immediately
    - skip: Skip this fix and continue
    - investigate: Launch deeper investigation of the code issue

    **For FIX MODIFIED verdicts:**
    - fix: Apply the modified code change
    - skip: Skip this fix and continue
    - investigate: Launch deeper investigation

    **For FIX NOT RECOMMENDED verdicts (finding incorrect, code is fine):**
    - accept: Accept that the code is correct as-is (default)
    - override: Apply the fix despite the recommendation
    - investigate: Launch investigation to reconsider
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
