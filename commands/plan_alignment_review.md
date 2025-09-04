# Plan Alignment Review

**CRITICAL** before doing anything else, read the contents of ~/.claude/commands/shared/review_commands.md and use the tagged sections wherever they are referenced.

<ExecutionSteps/>

<ReviewContext>
[PLAN_DOCUMENT] <PlanDocument/>
[REVIEW_TARGET]: alignment between [PLAN_DOCUMENT] and current git diff changes
[REVIEW_CONTEXT]: We are verifying that the actual implementation aligns with the plan. Compare specifications against implementation to identify missing features, unauthorized additions, and specification mismatches.
</ReviewContext>

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
    - **align to plan**: Modify code to match plan specification
    - **skip**: Skip this alignment and continue
    - **accept as built**: Update plan to document the deviation
    - **investigate**: Launch deeper investigation of the discrepancy
    
    **For ACCEPT RECOMMENDED verdicts:**
    - **accept as built**: Update plan to document the accepted deviation (default)
    - **align to plan**: Align code to plan anyway
    - **skip**: Skip without documenting
    - **investigate**: Launch deeper investigation
    
    **For DEFER RECOMMENDED verdicts:**
    - **skip**: Defer this alignment for later (default)
    - **align to plan**: Align code to plan now despite recommendation
    - **accept as built**: Update plan to document the deviation
    - **investigate**: Launch deeper investigation
</ReviewKeywords>

<ReviewFollowupParameters>
    When using ReviewFollowup from review_commands.md, substitute:
    - [EXPECTED_VERDICTS]: ALIGN RECOMMENDED, ACCEPT RECOMMENDED, or DEFER RECOMMENDED
</ReviewFollowupParameters>

<KeywordExecution>
    **CRITICAL**: Follow <PlanUpdateFormat/> from review_commands.md for all plan updates.
    
    **align to plan**: Use Edit tool to modify code to match the plan specification (apply suggested_code)
    **skip**: Mark as skipped and continue (maintain list for final summary)
    **accept as built**: Use Edit tool to update plan document using <AcceptAsBuiltTemplate/> format from review_commands.md
    **investigate**: Ask user "What specific aspect would you like me to investigate?", then launch Task tool with their focus
</KeywordExecution>
