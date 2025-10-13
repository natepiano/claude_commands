# Design Review

**MANDATORY FIRST STEP**:
1. Shared review commands: @~/.claude/shared/review_commands.md
2. Find and follow the <ExecutionSteps> section from that file
3. When you see tags like <ExecutionSteps/> below, these refer to sections in review_commands.md

<ReviewConfiguration>
MAX_FOLLOWUP_REVIEWS = 6
PERSONA_FILE = ~/.claude/shared/personas/architect_persona.md
</ReviewConfiguration>

<ExecutionSteps/>

<InitialReviewOutput>
Step 1: **Initial Design Review**
**Plan Document**: ${PLAN_DOCUMENT_RELATIVE}
**Max Followup Reviews**: ${MAX_FOLLOWUP_REVIEWS}
Now I'll begin the initial design review:
</InitialReviewOutput>

<DetermineReviewTarget>
**Execute this step to determine what to review (internal use only - do not output):**

PLAN_DOCUMENT = The absolute plan document path (from ongoing work context or $ARGUMENTS)
PLAN_DOCUMENT_RELATIVE = Convert PLAN_DOCUMENT to a path relative to the current working directory
REVIEW_TARGET = the feature design in ${PLAN_DOCUMENT}
REVIEW_CONTEXT = We are reviewing a FUTURE PLAN that has NOT been implemented yet. Our goal is to evaluate the DESIGN QUALITY of the proposed changes, NOT to check if they exist in current code.

Set PLAN_DOCUMENT using <PlanDocument/>
Calculate PLAN_DOCUMENT_RELATIVE as the relative path from current working directory
Set REVIEW_TARGET to: the feature design in ${PLAN_DOCUMENT}
Set REVIEW_CONTEXT to: We are reviewing a FUTURE PLAN that has NOT been implemented yet. Our goal is to evaluate the DESIGN QUALITY of the proposed changes, NOT to check if they exist in current code.

These are internal execution variables - only output PLAN_DOCUMENT_RELATIVE to the user per InitialReviewOutput template.
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

Review constraints are defined in: ~/.claude/shared/subagent_instructions/design_review_instructions.md

The instructions file is used by both initial review and investigation phases.
The main difference is how each phase handles validation failures:
- **Initial Review**: Discard findings that fail validation
- **Investigation**: Use REJECTED verdict for findings that fail validation

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
    - EXPECTED_VERDICTS: CONFIRMED, MODIFIED, or REJECTED
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

**Analysis**: ${issue}

**🔧 Recommended marker to add:**
${suggested_code}

**Why this strategy**: Plans with breaking changes, signature modifications, or tightly coupled updates typically need **Atomic**. Plans with independent features or gradual rollouts suit **Phased**.

**Verdict**: CONFIRMED
</MissingMigrationStrategyOutput>
</NamedFindingOutputTemplates>
