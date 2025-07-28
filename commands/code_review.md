For each area in the code base that has been defined for review, do the following:

<CodeReview>
    - Task a subagent to do a review and provide it the following context
    - it should examine just this area of the code base defined
    - it should focus on areas of complexity, duplication and code organization
    - it should provide a summary of the findings
    - the summary of the findings should be returned in priority order, the most critical issues first
    - the summary should include a report of issues in this format
    <IssueReport>
        ## Issue {number}: {short_description}
        ### Priority: {High | Medium | Low}
        ### Description:
        {expanded_description}
        ** current code **
        {current_code_sample}
        ** suggested code **
        {suggested_code_sample}
        ### Impact
        {impact}
    </IssueReport>
</CodeReview>

when all code reviews have been completed, create a unified summary, deduplicating results and limiting the results the the top 5 issues that are returned
