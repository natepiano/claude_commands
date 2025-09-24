# Code Review

**MANDATORY FIRST STEP**:
1. Use the Read tool to read /Users/natemccoy/.claude/shared/review_commands.md
2. Find and follow the <ExecutionSteps> section from that file
3. When you see tags like <ExecutionSteps/> below, these refer to sections in review_commands.md

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

## REVIEW CONSTRAINTS

<ReviewConstraints>
    - <RustIdiomsCompliance/>
    - <TypeSystemPrinciples/>
    - <CodeDuplicationDetection/>
</ReviewConstraints>

<RustIdiomsCompliance>
**MANDATORY CLIPPY COMPLIANCE CHECK**:
Before suggesting any Rust code changes, verify they align with current clippy lints:

1. **Functional Patterns (APPROVED by clippy)**:
   - `result.map_or_else(|e| error_case, |v| success_case)` - KEEP THIS
   - `option.map_or(default, |v| transform)` - KEEP THIS
   - `iterator.filter_map()` over `filter().map()` - KEEP THIS

2. **Pattern Matching**:
   - DO NOT suggest replacing functional patterns with verbose match statements
   - `match` is for complex control flow, not simple transformations

3. **Iterator Patterns**:
   - Prefer iterator combinators over manual loops
   - `collect()` when the full collection is needed

4. **Error Handling**:
   - `?` operator over explicit match on Result
   - `map_err()` for error transformation

**CRITICAL**: If unsure about a pattern, DO NOT suggest changes to idiomatic Rust code.
</RustIdiomsCompliance>


<CodeDuplicationDetection>
**MANDATORY CODE DUPLICATION DETECTION FOR CODE REVIEWS**:

1. **Types of Code Duplication to Detect**:

   a) **Identical Functions** - Multiple functions with same or nearly identical implementation
      - Copy-pasted functions with minor parameter differences
      - Functions that could be generalized with parameters
      - Utility functions scattered across modules

   b) **Logic Block Duplication** - Repeated code patterns within or across functions
      - Same validation logic in multiple places
      - Identical error handling blocks
      - Repeated data transformation patterns

   c) **Type/Structure Duplication** - Redundant data structures or types
      - Multiple structs representing the same concept
      - Enums with overlapping variants
      - Traits that duplicate behavior

   d) **Pattern Inconsistency** - Same functionality implemented different ways
      - Multiple approaches to same problem in the codebase
      - Inconsistent error handling strategies
      - Different state management patterns for similar use cases

2. **Resolution Requirements**:
   - If ANY duplication is detected, recommend consolidation
   - Extract common functionality into shared utilities
   - Choose ONE canonical implementation approach
   - Remove or refactor duplicate code paths

3. **Priority**:
   - All code duplication issues are HIGH priority
   - Code duplication creates maintenance burden
   - Inconsistent patterns confuse developers and create bugs
</CodeDuplicationDetection>

<ReviewKeywords>
    **For FIX RECOMMENDED verdicts:**
    - **fix**: Apply the suggested code change immediately
    - **skip**: Skip this fix and continue
    - **investigate**: Launch deeper investigation of the code issue

    **For FIX MODIFIED verdicts:**
    - **fix**: Apply the modified code change
    - **skip**: Skip this fix and continue
    - **investigate**: Launch deeper investigation

    **For FIX NOT RECOMMENDED verdicts (finding incorrect, code is fine):**
    - **accept**: Accept that the code is correct as-is (default)
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
