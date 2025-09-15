# Design Review

**MANDATORY FIRST STEP**:
1. Use the Read tool to read /Users/natemccoy/.claude/commands/shared/review_commands.md
2. Find and follow the <ExecutionSteps> section from that file
3. When you see tags like <ExecutionSteps/> below, these refer to sections in review_commands.md

<ExecutionSteps/>

<DetermineReviewTarget>
**Execute this step to determine what to review:**

Set [PLAN_DOCUMENT] using <PlanDocument/>
Set [REVIEW_TARGET] to: the feature design in [PLAN_DOCUMENT]
Set [REVIEW_CONTEXT] to: We are reviewing a plan to improve its design. Our goal is to identify gaps, over-engineering, and improvements to the plan.
</DetermineReviewTarget>


<ReviewCategories>
- **TYPE-SYSTEM**: Type system gaps - missing type-driven design opportunities in the plan
- **DESIGN**: Plan issues - architecture gaps and design completeness problems
- **IMPLEMENTATION**: Plan gaps - missing implementation details or considerations
- **SIMPLIFICATION**: Over-engineering in plan - unnecessarily complex approaches that could be simplified
</ReviewCategories>

<ReviewConstraints>
    - <SkipNotesCheck/>
    - <TypeSystemPrinciples/>
    - <AtomicChangeRequirement/>
    - <DuplicationPrevention/>
    - <DocumentComprehension/>
    - <DesignConsistency/>
</ReviewConstraints>

<ReviewKeywords>
    **For CONFIRMED verdicts:**
    - **agree**: Implement the confirmed design improvement
    - **skip**: Reject the suggestion - add to Skip Notes and continue
    - **skip silently**: Reject without updating the plan document
    - **skip with prejudice**: Permanently reject with ⚠️ PREJUDICE WARNING
    - **redundant**: Mark as redundant - the suggestion already exists in the plan
    - **investigate**: Launch deeper investigation of the design issue

    **For MODIFIED verdicts:**
    - **agree**: Implement the modified version of the suggestion
    - **skip**: Reject the modified suggestion - add to Skip Notes and continue
    - **skip silently**: Reject without updating the plan document
    - **skip with prejudice**: Permanently reject with ⚠️ PREJUDICE WARNING
    - **redundant**: Mark as redundant - the suggestion already exists in the plan
    - **investigate**: Launch deeper investigation of alternatives

    **For REJECTED verdicts:**
    - **override**: Override the rejection - treat as CONFIRMED and implement the suggestion
    - **agree**: Accept and document the rejection and continue (default)
    - **agree silently**: Accept the rejection without updating the plan document
    - **skip with prejudice**: Permanently reject with ⚠️ PREJUDICE WARNING
    - **redundant**: Mark as redundant - the suggestion already exists in the plan
    - **investigate**: Challenge the rejection and investigate further
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
    - **redundant**: Use Edit tool to add to "Design Review Skip Notes" section using <RedundantTemplate/> format from review_commands.md
    - **investigate**: Ask user "What specific aspect would you like me to investigate?", then launch Task tool with their focus
</KeywordExecution>
