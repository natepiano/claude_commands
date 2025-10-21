# Design Review

**MANDATORY FIRST STEP**:
Read and follow @~/.claude/shared/review_commands.md
When you see tags like <ExecutionSteps/> below, these refer to sections in that file.

<ReviewConfiguration>
MAX_FOLLOWUP_REVIEWS = 6
PERSONA_FILE = ~/.claude/shared/personas/architect_persona.md
</ReviewConfiguration>

Read and adopt persona from ${PERSONA_FILE}

<SharedWorkflows>
@~/.claude/shared/gap_analysis_workflow.md
</SharedWorkflows>

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

<FormatKeywords>
**Route to appropriate keywords based on verdict:**

If verdict is "CONFIRMED" or "MODIFIED":
    Use <DesignConfirmedKeywords/> from shared review_commands.md

If verdict is "REJECTED":
    Use <DesignRejectedKeywords/> from shared review_commands.md
</FormatKeywords>

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

## GAP ANALYSIS

<GapAnalysis>
    **When to run gap analysis:**
    - After completing all findings review in Step 5 (User Review)
    - Before presenting <FinalSummary/>
    - Always offered, regardless of whether IMPLEMENTATION-GAP findings were identified

    Display: "Would you like to run a comprehensive gap analysis?"
    Display: ""

    ## Available Actions
    - **analyze** - Run deep gap analysis with interactive review
    - **skip** - Complete the design review without gap analysis

    STOP and wait for user response.

    **If user says "analyze":**
    1. Set PLAN_DOCUMENT to the plan document path being reviewed
    2. Display: "🔍 Launching comprehensive gap analysis..."
    3. Use Task tool:
       - description: "Deep implementation gap analysis"
       - subagent_type: "general-purpose"
       - prompt: <GapAnalysisPrompt/> (from shared workflow)
    4. Parse response. If gaps found:
       - Display summary with gap count and severity breakdown
       - Execute <GapReview/> (from shared workflow) with parsed gaps array
    5. After gap review completes, proceed to <FinalSummary/>

    **If user says "skip":**
    - Proceed directly to <FinalSummary/>

    Execute <ValidateUserResponse/> with:
        expected_keywords: [analyze, skip]
        option_descriptions: [
            "- **analyze** - Run deep gap analysis with interactive review",
            "- **skip** - Complete the design review without gap analysis"
        ]
</GapAnalysis>

