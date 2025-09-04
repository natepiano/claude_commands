# Design Review

**CRITICAL** before doing anything else, read the contents of ~/.claude/commands/shared/review_commands.md and use the tagged sections wherever they are referenced.

<ExecutionSteps/>

<ReviewContext>
[PLAN_DOCUMENT] <PlanDocument/>
[REVIEW_TARGET]: the feature design in [PLAN_DOCUMENT]
[REVIEW_CONTEXT]: We are reviewing a plan to improve its design. Our goal is to identify gaps, over-engineering, and improvements to the plan.
</ReviewContext>


<ReviewCategories>
- **TYPE-SYSTEM**: Type system gaps - missing type-driven design opportunities in the plan
- **DESIGN**: Plan issues - architecture gaps and design completeness problems
- **IMPLEMENTATION**: Plan gaps - missing implementation details or considerations
- **SIMPLIFICATION**: Over-engineering in plan - unnecessarily complex approaches that could be simplified
</ReviewCategories>

<ReviewConstraints>
    - <SkipNotesCheck/>
    - <TypeSystemPrinciples/>
    - <DocumentComprehension/>
    - <DesignConsistency/>
</ReviewConstraints>

<ReviewKeywords>
    **For CONFIRMED verdicts:**
    - **agree**: Update plan document with the suggested design improvement
    - **skip**: Add to "Design Review Skip Notes" section and continue
    - **skip with prejudice**: Permanently reject with ⚠️ PREJUDICE WARNING
    - **investigate**: Launch deeper investigation of the design issue

    **For MODIFIED verdicts:**
    - **agree**: Update plan document with the modified version
    - **skip**: Add to "Design Review Skip Notes" section and continue
    - **skip with prejudice**: Permanently reject with ⚠️ PREJUDICE WARNING
    - **investigate**: Launch deeper investigation

    **For REJECTED verdicts:**
    - **skip**: Accept the rejection and continue (default)
    - **skip with prejudice**: Permanently reject with ⚠️ PREJUDICE WARNING
    - **investigate**: Launch investigation to reconsider
</ReviewKeywords>

<ReviewFollowupParameters>
    When using ReviewFollowup from review_commands.md, substitute:
    - [EXPECTED_VERDICTS]: CONFIRMED, MODIFIED, or REJECTED
</ReviewFollowupParameters>

<KeywordExecution>
    **CRITICAL**: Follow <PlanUpdateFormat/> from review_commands.md for all plan updates.
    
    **agree**: Use Edit tool to add to plan document using <AgreeTemplate/> format from review_commands.md
    **skip**: Use Edit tool to add to "Design Review Skip Notes" section using <SkipTemplate/> format from review_commands.md
    **skip with prejudice**: Use Edit tool to add to "Design Review Skip Notes" section using <SkipWithPrejudiceTemplate/> format from review_commands.md
    **investigate**: Ask user "What specific aspect would you like me to investigate?", then launch Task tool with their focus
</KeywordExecution>
