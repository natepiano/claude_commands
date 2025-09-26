# Design Review

**MANDATORY FIRST STEP**:
1. Use the Read tool to read /Users/natemccoy/.claude/shared/review_commands.md
2. Find and follow the <ExecutionSteps> section from that file
3. When you see tags like <ExecutionSteps/> below, these refer to sections in review_commands.md

<ExecutionSteps/>

<ReviewPersona>
You are a principal system architect specializing in design review with deep expertise in:
- Type-driven architecture and domain modeling
- Distributed systems design and scalability patterns
- API contract design and backwards compatibility
- Implementation feasibility and technical debt assessment
- Cross-cutting concerns like monitoring, logging, and error handling

Your expertise allows you to:
- Identify missing architectural components before implementation begins
- Spot type system opportunities that prevent entire categories of bugs
- Recognize design patterns that will cause implementation difficulties
- Detect incomplete specifications that will block developers
- Evaluate designs for long-term maintainability and evolution

Review with the foresight of someone who has seen countless projects fail due to poor initial design decisions.
</ReviewPersona>

<InitialReviewOutput>
Step 1: Initial Design Review

  Plan Document: [PLAN_DOCUMENT]

  Now I'll launch the Task tool for the initial review:
</InitialReviewOutput>

<DetermineReviewTarget>
**Execute this step to determine what to review:**

Set [PLAN_DOCUMENT] using <PlanDocument/>
Set [REVIEW_TARGET] to: the feature design in [PLAN_DOCUMENT]
Set [REVIEW_CONTEXT] to: We are reviewing a FUTURE PLAN that has NOT been implemented yet. Our goal is to evaluate the DESIGN QUALITY of the proposed changes, NOT to check if they exist in current code.
</DetermineReviewTarget>


<ReviewCategories>
- **TYPE-SYSTEM**: Type system gaps - missing type-driven design opportunities in the plan
- **DESIGN**: Plan issues - architecture gaps and design completeness problems
- **IMPLEMENTATION**: Plan gaps - missing implementation details or considerations
- **IMPLEMENTATION-GAP**: Missing implementation steps - goals, use cases, or requirements stated in the plan that lack corresponding concrete implementation details
- **SIMPLIFICATION**: Over-engineering in plan - unnecessarily complex approaches that could be simplified
</ReviewCategories>

<NamedFindings>
Registry of named findings that bypass investigation due to self-evident violations:

- **line_number_violation**: Line number references in design documents
  - Auto-verdict: CONFIRMED
  - Output template: LineNumberViolationOutput
  - Detection: Any reference like "line 123", "lines 45-67", etc. in plan text

- **missing_migration_strategy**: Design plan lacks required Migration Strategy marker
  - Auto-verdict: CONFIRMED
  - Output template: MissingMigrationStrategyOutput
  - Detection: Plan document missing both "**Migration Strategy: Atomic**" and "**Migration Strategy: Phased**"
</NamedFindings>

<NamedFindingDetection>
**CRITICAL**: When detecting violations that match patterns in <NamedFindings/>, you MUST:
1. Include the standard finding fields as usual
2. ADD a "named_finding" field to your JSON with the appropriate value
3. For line number violations per <LineNumberProhibition/>, set: "named_finding": "line_number_violation"
4. For missing migration strategy per <AtomicChangeRequirement/>, set: "named_finding": "missing_migration_strategy"
5. Named findings will skip investigation as the violation is self-evident
</NamedFindingDetection>

## REVIEW CONSTRAINTS

<ReviewConstraints>
    - <SkipNotesCheck/>
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


<SkipNotesCheck>
**MANDATORY** DO THIS FIRST
Check for a "Design Review Skip Notes" section in the document:
1. Read every single skipped item to understand rejection reasons
2. Cross-reference your ideas against previously rejected concepts
3. Do not re-suggest items marked with "⚠️ PREJUDICE WARNING"
4. Only proceed after confirming recommendations don't duplicate rejected items
</SkipNotesCheck>

<DocumentComprehension>
For plan document reviews:
1. Read the entire plan from beginning to end before making recommendations
2. **MANDATORY REDUNDANCY CHECK**: Before suggesting ANY fix or improvement:
   - Search the ENTIRE plan for "Proposed Fix", "Solution", "Implementation", or similar sections
   - Check if your suggested code already exists ANYWHERE in the plan (even partially)
   - If found, you MUST mark as REDUNDANT, not CONFIRMED
   - Quote the exact section containing the existing solution when claiming redundancy
3. **CRITICAL**: A finding is REDUNDANT if:
   - The plan already contains the same or similar code solution
   - The plan already describes fixing this exact issue
   - The "Proposed Fix" section addresses the problem you identified
   - Even if the current code doesn't have it yet (remember: plans describe FUTURE changes)
4. Quote specific sections when claiming gaps exist
5. Understand how topics connect across different sections
6. For every "missing" claim, either quote the section that should contain it or explain why existing content is insufficient
7. **NEVER** suggest code that already appears in the plan's "Proposed Fix" or solution sections
</DocumentComprehension>

<DesignConsistency>
For design document reviews:
1. **Internal Consistency**: Verify that all sections of the plan align with each other
2. **Decision Alignment**: Check that design decisions don't contradict across sections
3. **Terminology Consistency**: Ensure the same terms are used consistently throughout
4. **Architectural Coherence**: Verify that the overall architecture remains coherent
5. **Example Consistency**: Ensure code examples match the described approach
6. **Impact Analysis**: Flag when changes in one section require updates to others
</DesignConsistency>

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

4. **Priority**: All IMPLEMENTATION-GAP issues are HIGH priority
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

<ReviewKeywords>
    **For CONFIRMED verdicts:**
    - agree: Implement the confirmed design improvement
    - skip: Reject the suggestion - add to Skip Notes and continue
    - skip silently: Reject without updating the plan document
    - skip with prejudice: Permanently reject with ⚠️ PREJUDICE WARNING
    - redundant: Mark as redundant - the suggestion already exists in the plan
    - investigate: Launch deeper investigation of the design issue

    **For MODIFIED verdicts:**
    - agree: Implement the modified version of the suggestion
    - skip: Reject the modified suggestion - add to Skip Notes and continue
    - skip silently: Reject without updating the plan document
    - skip with prejudice: Permanently reject with ⚠️ PREJUDICE WARNING
    - redundant: Mark as redundant - the suggestion already exists in the plan
    - investigate: Launch deeper investigation of alternatives

    **For REJECTED verdicts (finding is wrong, plan is correct):**
    - override: Override the rejection - treat as CONFIRMED and implement the suggestion
    - agree: Accept that the finding was incorrect - plan stays unchanged (default)
    - agree silently: Accept the rejection without updating the plan document
    - skip with prejudice: Permanently reject with ⚠️ PREJUDICE WARNING
    - investigate: Challenge the rejection and investigate further
</ReviewKeywords>

<ReviewFollowupParameters>
    When using ReviewFollowup from review_commands.md, substitute:
    - [EXPECTED_VERDICTS]: CONFIRMED, MODIFIED, or REJECTED
</ReviewFollowupParameters>

<KeywordExecution>
    **CRITICAL**: For **agree** keyword, do NOT add verdict sections to the plan - just update the plan to match the agreed suggestion.

    - **agree**:
      - For CONFIRMED/MODIFIED verdicts: Use Edit tool to update the plan document directly with the suggested changes (no verdict sections added)
      - For REJECTED verdicts: Use Edit tool to add to "Design Review Skip Notes" section using <SkipTemplate/> format from review_commands.md (agreeing with the rejection)
    - **agree silently**: (For REJECTED verdicts) Skip without any plan updates - continue to next finding
    - **override**: (For REJECTED verdicts) Use Edit tool to update the plan document directly with the suggested changes (no verdict sections added) - treat as if verdict was CONFIRMED
    - **skip**: Use Edit tool to add to "Design Review Skip Notes" section using <SkipTemplate/> format from review_commands.md
    - **skip silently**: Skip without any plan updates - continue to next finding
    - **skip with prejudice**: Use Edit tool to add to "Design Review Skip Notes" section using <SkipWithPrejudiceTemplate/> format from review_commands.md
    - **redundant**: Use Edit tool to add to "Design Review Skip Notes" section using <RedundantTemplate/> format from review_commands.md (only for CONFIRMED/MODIFIED verdicts)
    - **investigate**: Ask user "What specific aspect would you like me to investigate?", then launch Task tool with their focus
</KeywordExecution>

<NamedFindingOutputTemplates>
Specialized output templates for named findings that bypass investigation:

<LineNumberViolationOutput>
# **${id}**: Line Number References Detected (${current_number} of ${total_findings})

**❌ Issue**: Line numbers in design documents become stale immediately
**📍 Location**: ${location.plan_reference}
**🔧 Fix**: Replace with structural references (sections, function names, landmarks)

**Current**: `${current_code}`
**Instead use**: `${suggested_code}`

**Verdict**: CONFIRMED
</LineNumberViolationOutput>

<MissingMigrationStrategyOutput>
# **${id}**: Missing Migration Strategy (${current_number} of ${total_findings})

**❌ Issue**: Design plan missing required migration strategy marker
**📍 Location**: ${location.plan_reference}

**🔧 Choose ONE marker to add:**
- `**Migration Strategy: Atomic**` - All-at-once implementation
- `**Migration Strategy: Phased**` - Multi-step implementation with phases

**Verdict**: CONFIRMED
</MissingMigrationStrategyOutput>
</NamedFindingOutputTemplates>
