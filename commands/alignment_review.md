# Plan Alignment Review

**CRITICAL** before doing anything else, read the contents of ~/.claude/commands/shared/review_commands.md and use the tagged sections wherever they are referenced.

<ExecutionSteps/>

<DetermineReviewTarget>
**Execute this step to determine what to review:**

Set [PLAN_DOCUMENT] using <PlanDocument/>
Set [REVIEW_TARGET] to: alignment between [PLAN_DOCUMENT] and current git diff changes
Set [REVIEW_CONTEXT] to: We are verifying that the actual implementation aligns with the plan. Compare specifications against implementation to identify missing features, unauthorized additions, and specification mismatches.
</DetermineReviewTarget>

<ReviewCategories>
- **MISSING**: Planned features not implemented - functionality specified in plan but absent from code
- **MISMATCH**: Implementation differs from plan - code approach conflicts with specified design
- **PARTIAL**: Incomplete implementation - features partially implemented compared to plan scope
- **UNPLANNED**: Additions beyond plan - functionality implemented that wasn't specified
- **SPECIFICATION**: Requirement violations - implementation doesn't meet specified requirements
</ReviewCategories>

<ReviewConstraints>
    - <PlanDocumentAnalysis/>
    - <ImplementationMapping/>
</ReviewConstraints>

<ReviewKeywords>
    **For ALIGN RECOMMENDED verdicts:**
    - **align to plan**: Implement the recommended code alignment
    - **skip**: Reject the alignment recommendation - document and continue
    - **skip silently**: Reject without updating the plan document
    - **accept as built**: Accept the deviation - update plan to document it
    - **investigate**: Launch deeper investigation of the discrepancy
    
    **For ACCEPT RECOMMENDED verdicts:**
    - **accept as built**: Accept the recommendation - update plan to document the deviation (default)
    - **align to plan**: Override recommendation - align code to plan anyway
    - **skip**: Reject the acceptance recommendation - document and continue
    - **skip silently**: Reject without updating the plan document
    - **investigate**: Launch deeper investigation of alternatives
    
    **For DEFER RECOMMENDED verdicts:**
    - **skip**: Accept the deferral recommendation - document for later (default)
    - **skip silently**: Accept the deferral without updating the plan document
    - **align to plan**: Override recommendation - align code to plan now
    - **accept as built**: Override recommendation - accept deviation and document it
    - **investigate**: Challenge the deferral and investigate further
</ReviewKeywords>

<ReviewFollowupParameters>
    When using ReviewFollowup from review_commands.md, substitute:
    - [EXPECTED_VERDICTS]: ALIGN RECOMMENDED, ACCEPT RECOMMENDED, or DEFER RECOMMENDED
</ReviewFollowupParameters>

<KeywordExecution>
    **CRITICAL**: Follow <PlanUpdateFormat/> from review_commands.md for all plan updates.
    
    **align to plan**: Use Edit tool to modify code to match the plan specification (apply suggested_code)
    **skip**: Mark as skipped and continue (maintain list for final summary)
    **skip silently**: Skip without any plan updates - continue to next finding
    **accept as built**: Use Edit tool to update plan document using <AcceptAsBuiltTemplate/> format from review_commands.md
    **investigate**: Ask user "What specific aspect would you like me to investigate?", then launch Task tool with their focus
</KeywordExecution>
