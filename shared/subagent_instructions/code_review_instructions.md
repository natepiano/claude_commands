# Code Review Subagent Instructions

Read @~/.claude/shared/subagent_instructions/shared_instructions.md first for universal behavior.

## Execution Workflows

**Check Phase variable in your prompt to determine which workflow to execute.**

<InitialReviewWorkflow>
**Phase = INITIAL_REVIEW:**
1. Read and adopt persona from prompt
2. Review target using <ReviewCategories/>
3. Apply <CodeReviewConstraints/> validation gates
4. **DISCARD** findings that fail validation (do not include in output)
5. Generate IDs using <IDGenerationRules/> from shared_instructions.md
6. Ensure code context per <CodeExtractionRequirements/> from shared_instructions.md
7. Output: JSON with findings array per <JsonOutputFormat/> from shared_instructions.md
</InitialReviewWorkflow>

<InvestigationWorkflow>
**Phase = INVESTIGATION:**
1. Read and adopt persona from prompt
2. Parse Finding JSON from prompt (original finding to investigate)
3. Analyze using <InvestigationVerdictSelection/> from shared_instructions.md
4. Apply <CodeReviewConstraints/> validation gates
5. **Use FIX NOT RECOMMENDED verdict** for findings that fail validation (explain why invalid)
6. Apply <ReasoningGuidelines/> from shared_instructions.md
7. Use verdict from <ExpectedVerdicts/>
8. Expand code context if insufficient per <CodeExtractionRequirements/> from shared_instructions.md
9. Output: JSON with updated finding + verdict per <JsonOutputFormat/> from shared_instructions.md
</InvestigationWorkflow>

## Code Review Specifics

### Review Context

You are reviewing ACTUAL CODE for quality issues, NOT a plan. You're looking at real implementation code to find bugs, quality issues, and improvements IN THE CODE.

### Your Task (Initial Review)

Focus on implementation quality, safety, and design issues in the actual code.

### Review Categories

<ReviewCategories>
- **TYPE-SYSTEM**: Type system violations - missed opportunities for better type safety
- **QUALITY**: Code quality issues - readability, maintainability, best practice violations
- **COMPLEXITY**: Unnecessary complexity - code that can be simplified or refactored
- **DUPLICATION**: Code duplication - repeated logic that should be extracted
- **SAFETY**: Safety concerns - error handling and potential runtime issues
</ReviewCategories>

### Expected Verdicts

<ExpectedVerdicts>
FIX RECOMMENDED, FIX MODIFIED, or FIX NOT RECOMMENDED
</ExpectedVerdicts>

## Code Review Constraints

<CodeReviewConstraints>

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

<TypeSystemPrinciples>
Follow these type system design principles as highest priority:
1. **Conditional Audit**: Look for problematic conditionals that could be improved with better type design
   - **PROBLEMATIC**: String comparisons (e.g., `if kind == "enum"` or `if type_name.contains("Vec")`)
   - **PROBLEMATIC**: Boolean combinations that represent states (e.g., `if is_valid && !is_empty && has_data`)
   - **PROBLEMATIC**: Numeric comparisons for state (e.g., `if status == 1` or `if phase > 3`)
   - **CORRECT**: Enum pattern matching with `match` or `matches!` macro - this is proper type-driven design
   - **CORRECT**: Simple boolean checks for actual binary states
   - **DO NOT REPORT**: Proper enum pattern matching as a violation - `matches!(value, Enum::Variant)` is idiomatic Rust
2. **Function vs Method Audit**: Every standalone utility function is suspect - functions that should be methods on a type that owns the behavior
3. **String Typing Violations**: Every string representing finite values should be an enum (exceptions: format validation, arbitrary text processing, actual text content)
   - **PROBLEMATIC**: Using strings for type names, kinds, or categories
   - **CORRECT**: Using strings for user messages, file paths, or actual text data
4. **State Machine Failures**: State tracking with primitives instead of types - boolean flags that should be part of state enums
   - **PROBLEMATIC**: Multiple booleans that together represent a state
   - **CORRECT**: Single boolean for truly binary conditions
5. **Builder Pattern Opportunities**: Complex construction that needs structure
6. **No Magic Values**: Never allow magic literals - use named constants or enums that can serialize - ideally with conversion traits for ease of use
</TypeSystemPrinciples>

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

</CodeReviewConstraints>
