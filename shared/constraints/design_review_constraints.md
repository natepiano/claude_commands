# Design Review Constraints

---

<ReviewConstraints>
  - <PlanComprehensionPhase/>
  - <SkipNotesCheck/>
  - <FindingValidationGates/>
  - <PlanTypeReviewPrinciples/>
  - <TypeSystemPrinciples/>
  - <AtomicChangeRequirement/>
  - <DuplicationPrevention/>
  - <DocumentComprehension/>
  - <DesignConsistency/>
  - <PlanNotImplementation/>
  - <ImplementationCoverageCheck/>
  - <ImplementationSpecificity/>
  - <LineNumberProhibition/>
</ReviewConstraints>

---

## Constraint Definitions

<PlanComprehensionPhase>

**MANDATORY PRE-WORK - COMPLETE BEFORE GENERATING ANY FINDINGS**:

The review subagent MUST complete these steps internally BEFORE generating any findings:

**Step 1: Read the Complete Plan**
- Use Read tool to read the entire plan document from start to finish
- Do NOT skim or keyword search - read every section

**Step 2: Extract Plan Fundamentals (internal analysis - do not output to user)**
Determine the following internally to guide your review:
- **Purpose**: What is this plan trying to achieve? (1-2 sentences)
- **Scope**: What specific area/component does this change? (list)
- **In-Scope**: What does the plan explicitly address? (list)
- **Out-of-Scope**: What does the plan explicitly exclude? (list from plan or inferred)
- **Plan Type**: Classify as ONE of:
  * Internal Refactoring (code structure improvement, no external API changes)
  * API Design (new or modified public APIs)
  * Feature Implementation (new user-facing functionality)
  * Documentation (type guides, examples, internal docs)
  * Bug Fix (correcting incorrect behavior)

**Step 3: Review Approach Selection (internal - do not output)**
Based on plan type, internally determine which review principles apply:
- Internal Refactoring → Focus on: consistency, duplication elimination, architectural coherence
- API Design → Focus on: type safety, error handling, backward compatibility
- Feature Implementation → Focus on: completeness, user experience, edge cases
- Documentation → Focus on: accuracy, clarity, completeness of examples
- Bug Fix → Focus on: root cause addressed, no regressions, test coverage

**Step 4: Terminology Check (internal - do not output)**
Internally identify any domain-specific terms and their meaning in THIS plan's context.

**CRITICAL**: Complete these steps internally to inform your review approach, then proceed directly to generating findings JSON. Do NOT output this analysis to the user - it is internal pre-work only.
</PlanComprehensionPhase>

<SkipNotesCheck>
**MANDATORY** DO THIS FIRST
Check for a "Design Review Skip Notes" section in the document:
1. Read every single skipped item to understand rejection reasons
2. Cross-reference your ideas against previously rejected concepts
3. Do not re-suggest items marked with "⚠️ PREJUDICE WARNING"
4. Only proceed after confirming recommendations don't duplicate rejected items
</SkipNotesCheck>

<FindingValidationGates>
**VALIDATE EVERY FINDING AGAINST ALL GATES**:

For EACH potential finding, verify all gates. The calling workflow determines what happens when validation fails.

**Gate 1: Evidence Verification**
- If location.code_file is specified:
  * Use Read tool to verify file exists
  * Extract ACTUAL code snippet for current_code
  * Verify the file exists OR is marked as "PLANNED NEW FILE"
- If location.plan_reference cites a section name:
  * Use Grep to search plan for that section heading
  * Confirm section discusses the topic claimed
  * Verify section exists and is relevant

**Gate 2: Scope Verification**
- Verify the concern is within the plan's stated scope
- Confirm the plan claims to address this area
- Exception: Scope gap issues (where you're flagging missing scope)

**Gate 3: Plan Type Alignment**
- Verify finding matches the review approach for this plan type
- Example: Don't apply API design principles to documentation plans
- Confirm alignment with plan type from <PlanComprehensionPhase/>

**Gate 4: Not Already Addressed**
- Check if the plan already correctly addresses this issue
- Use <GrepForPlanRedundancy/> procedure
- Verdict guidance for investigation phase:
  * Plan has correct solution → suggests REJECTED verdict
  * Plan addresses but solution wrong/incomplete → suggests MODIFIED verdict
  * Plan doesn't address it → suggests CONFIRMED verdict

**Gate 5: Not In Skip Notes**
- Check if this suggestion appears in "Design Review Skip Notes"
- Verify not marked with "⚠️ PREJUDICE WARNING"
- Confirm not previously rejected

**Gate 6: Actual Problem**
- Verify this is a real design issue, not a misunderstanding
- Confirm it aligns with plan type review principles
- Validate against constraints in this file
</FindingValidationGates>

<PlanTypeReviewPrinciples>
**TAILOR YOUR REVIEW TO THE PLAN TYPE**:

**For Internal Refactoring Plans**:
✓ DO review: Code duplication, consistency, architectural coherence
✓ DO check: Data structure design, function organization
✗ DON'T expect: User documentation, usage examples, backward compatibility concerns
✗ DON'T suggest: New user-facing features, API expansions

**For API Design Plans**:
✓ DO review: Type safety, error handling, backward compatibility
✓ DO check: API surface design, contract clarity
✗ DON'T focus on: Internal implementation details, refactoring opportunities

**For Documentation Plans**:
✓ DO review: Accuracy, clarity, completeness of examples
✓ DO check: Example code correctness, coverage of use cases
✗ DON'T suggest: Implementation changes, type system redesigns
✗ DON'T apply: Runtime validation concerns (handled by subject code)

**For Feature Implementation Plans**:
✓ DO review: Completeness, user experience, edge cases
✓ DO check: Integration points, migration strategy
✗ DON'T confuse: Internal tooling plans with user-facing features

**For Bug Fix Plans**:
✓ DO review: Root cause analysis, regression prevention
✓ DO check: Test coverage, edge case handling
✗ DON'T suggest: Architectural redesigns beyond the fix scope

**CRITICAL**: Applying the wrong review lens is a category error that invalidates findings.
Always reference the Plan Type from <PlanComprehensionPhase/> when evaluating findings.
</PlanTypeReviewPrinciples>

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

<AtomicChangeRequirement>
**MIGRATION STRATEGY COMPLIANCE**: Check the plan document for a Migration Strategy marker:

- If you find "**Migration Strategy: Atomic**": Plans must represent complete, indivisible changes. Reject any suggestions for incremental rollouts, backward compatibility, gradual migrations, or hybrid approaches. Either keep current design unchanged OR replace it entirely - no middle ground.

- If you find "**Migration Strategy: Phased**": The plan has explicitly chosen a phased approach. Validate that the phased implementation makes sense and provides appropriate review points and validation steps between phases.

- If neither marker is present: **Default to Atomic** - Apply the atomic change requirements above.

**No Hybrid Approaches**: Do not suggest mixing atomic and phased strategies within the same plan. The migration strategy choice applies to the entire plan.
</AtomicChangeRequirement>

<DuplicationPrevention>
**MANDATORY DUPLICATION DETECTION AND ELIMINATION FOR PLAN DOCUMENTS**:

1. **Types of Duplication to Detect**:

   a) **Existing Duplication** - Already present in the code area being modified
      - Multiple functions doing the same thing
      - Repeated logic across methods
      - The plan should consolidate these, not perpetuate them

   b) **Plan-Introduced Identical** - Plan creates exact copies
      - New function that duplicates an existing function
      - Copy-pasted logic with minor variations
      - Redundant data structures or types
      - Inconsistent reimplementation of existing patterns

   c) **Plan-Introduced Overlap** - Plan creates parallel/competing paths
      - New call flow that overlaps with existing flow
      - Alternative way to accomplish same goal
      - Multiple entry points for same functionality
      - Conflicting approaches to similar problems

2. **Resolution Requirements**:
   - If ANY duplication is detected, the plan MUST be redesigned
   - No "letting them coexist" - eliminate the duplication
   - Choose ONE approach: enhance existing OR fully replace
   - Create a single source of truth

3. **Priority**:
   - All duplication issues are HIGH priority
   - Duplication compounds over time into technical debt
   - Prevention now saves major refactoring later
</DuplicationPrevention>

<DocumentComprehension>
For plan document reviews:
1. Read the entire plan from beginning to end before making recommendations
2. Before suggesting any fix or improvement:
   - Search the plan for "Proposed Fix", "Solution", "Implementation", or similar sections
   - Check if your suggested code already exists in the plan (even partially)
   - Use <RedundancyCriteria/> and <GrepForPlanRedundancy/> to evaluate the finding
3. Quote specific sections when claiming gaps exist
4. Understand how topics connect across different sections
5. For every "missing" claim, either quote the section that should contain it or explain why existing content is insufficient
</DocumentComprehension>

<RedundancyCriteria>
**Understanding redundant findings:**

A finding is REDUNDANT when:
- The plan already contains the same or similar code solution
- The plan already describes fixing this exact issue
- A "Proposed Fix" or "Implementation" section addresses the problem you identified
- Your suggested code already appears in the plan (even partially)

**Important context:**
- Plans describe FUTURE changes, so current code NOT matching the plan is EXPECTED
- Don't confuse "current code has a problem" with "plan doesn't address it"
- The redundancy check looks at what the PLAN proposes, not what exists in current code

**How to document redundancy:**
In the redundancy_check field, quote the section where the plan addresses this issue.
</RedundancyCriteria>

<GrepForPlanRedundancy>
**How to check if the plan already addresses an issue:**

**Procedure:**
1. Use Grep to search PLAN DOCUMENT for keywords from your concern
   - Example: Concern about Ord derives → search: "Ord|PartialOrd|VariantName"
2. If Grep finds matches, Read those sections to verify what the plan proposes
3. Extract the exact quote and location that supports your assessment
4. Fill out the redundancy_check field in your JSON based on what you find

**MANDATORY redundancy_check JSON fields:**

```json
"redundancy_check": {
  "grep_performed": true,
  "grep_pattern": "Hash|Ord|VariantName",
  "grep_results_summary": "Found 3 matches in Phase 1a section",
  "plan_addresses_this": "YES_IDENTICAL",
  "supporting_quote": "#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]",
  "quote_location": "Phase 1a code block, VariantName struct definition",
  "assessment": "REDUNDANT"
}
```

**Field Requirements:**

1. **grep_pattern** (required): The exact pattern you searched for
2. **grep_results_summary** (required): Brief summary of what grep found
3. **supporting_quote** (required):
   - For "YES_IDENTICAL" or "YES_DIFFERENT": Quote the plan text that addresses this issue
   - For "NO": Quote the section where this SHOULD have been mentioned but wasn't
4. **quote_location** (required): Section name and context (e.g., "Phase 1a, VariantName definition")
5. **plan_addresses_this** (required): "YES_IDENTICAL", "YES_DIFFERENT", or "NO"
6. **assessment** (required): "REDUNDANT", "ALTERNATIVE_NEEDED", or "GAP"

**Decision Guide for redundancy_check.assessment:**

```
Plan proposes correct solution
  → assessment: "REDUNDANT"
  → plan_addresses_this: "YES_IDENTICAL"
  → supporting_quote: Extract the exact code/text from the plan showing the solution
  → quote_location: Cite the specific section

Plan proposes a solution BUT it's wrong or incomplete
  → assessment: "ALTERNATIVE_NEEDED"
  → plan_addresses_this: "YES_DIFFERENT"
  → supporting_quote: Extract what the plan currently proposes
  → quote_location: Cite where the incomplete solution appears

Plan doesn't mention this issue at all
  → assessment: "GAP"
  → plan_addresses_this: "NO"
  → supporting_quote: Quote the section where this SHOULD appear but doesn't
  → quote_location: Cite the section that lacks this information
```

**CRITICAL ENFORCEMENT:**
- You CANNOT claim a plan lacks something if you cannot provide a supporting_quote showing the absence or inadequacy
- The supporting_quote must be extracted from the actual plan document using Read tool
- If your supporting_quote contradicts your assessment, your finding is INVALID and must be DISCARDED
</GrepForPlanRedundancy>

<DesignConsistency>
For design document reviews:
1. **Internal Consistency**: Verify that all sections of the plan align with each other
2. **Decision Alignment**: Check that design decisions don't contradict across sections
3. **Terminology Consistency**: Ensure the same terms are used consistently throughout
4. **Architectural Coherence**: Verify that the overall architecture remains coherent
5. **Example Consistency**: Ensure code examples match the described approach
6. **Impact Analysis**: Flag when changes in one section require updates to others
</DesignConsistency>

<PlanNotImplementation>
**CRITICAL - THIS IS A PLAN REVIEW, NOT A CODE AUDIT**:
The plan describes FUTURE changes that haven't been implemented yet.

NEVER report as issues:
- "Proposed types/functions don't exist in codebase"
- "Current code doesn't match the plan"
- "Planned changes haven't been made"

ONLY evaluate:
- Is the plan internally consistent?
- Are the proposed changes well-designed?
- Will the plan achieve its stated goals?
- Are there better approaches?
</PlanNotImplementation>

<ImplementationCoverageCheck>
**MANDATORY IMPLEMENTATION COVERAGE ANALYSIS**:
Every stated goal, use case, requirement, or necessary feature MUST have corresponding implementation steps.

**CRITICAL - Plan Type Matters**:
- **Internal Refactoring**: Don't look for user documentation or usage examples - check technical goals only
- **Documentation Plans**: Don't look for feature implementations - check documentation completeness
- **API Design**: Look for contract specifications, not internal implementation details
- **Feature Implementation**: Check user-facing goals against implementation sections
- **NEVER** flag missing features that were never in the plan's stated scope

1. **Extract All Commitments**: Identify every:
   - Stated goal or objective
   - Use case or user story
   - Requirement (functional or non-functional)
   - "Must have" or "should support" statement
   - Example usage that implies functionality
   - "The system will..." or "This enables..." statements

2. **Verify Coverage**: For each commitment, check:
   - Implementation section exists for this feature
   - No goals left unaddressed
   - All use cases have corresponding sections
   - Requirements map to concrete plans

3. **Flag Gaps as IMPLEMENTATION-GAP Issues**: Report when:
   - A goal has no implementation section
   - A use case lacks corresponding code changes
   - A requirement is stated but not addressed
   - Examples show functionality not in implementation
   - "Future work" items that should be current scope

4. **DO NOT Flag**:
   - User-facing examples in internal refactoring plans (not needed)
   - Implementation details in documentation plans (out of scope)
   - Features never claimed by the plan (scope creep)

5. **Priority**: All IMPLEMENTATION-GAP issues are HIGH priority
   - These represent broken promises
   - Users expect these features based on the plan
   - Missing these undermines trust
</ImplementationCoverageCheck>

<ImplementationSpecificity>
**MANDATORY**: All proposed code changes must specify:
1. **File path**: Full relative path from project root (e.g., `src/types.rs`)
2. **Target element**: Function/type/trait being modified (e.g., `validate_input()`)
3. **Context markers**: Parent function/unique patterns since line numbers shift with edits
4. **Concrete changes**: Not "update validation" but "replace string comparison with enum match"
5. **New code location**: For additions, specify where (e.g., "after ValidationError enum")
</ImplementationSpecificity>

<LineNumberProhibition>
**CRITICAL - NO LINE NUMBERS IN DESIGN DOCUMENTS**:
Design documents must NEVER contain line number references because they become stale immediately as code evolves.

**PROHIBITED PATTERNS**:
- "Add after line 64"
- "Insert at line 429"
- "Between lines 66-98"
- "See line 312 for context"

**REQUIRED ALTERNATIVES**:
- **Section references**: "Add to Section: Type Definitions"
- **Structural landmarks**: "After the VariantSignature Display implementation"
- **Function scope**: "Add to the validate_input() function"
- **Code patterns**: "Insert after the MutationStatus enum definition"
- **Relative positioning**: "Before the MutationPathInternal struct"

**ENFORCEMENT**:
- Flag ANY line number reference in plans as a DESIGN issue
- Suggest conversion to structural references
- Line numbers may only appear in JSON location fields for actual code files
- ALL plan text must use structural/semantic references

**RATIONALE**: Line numbers change with every edit, making design documents immediately obsolete and causing implementation confusion.
</LineNumberProhibition>
