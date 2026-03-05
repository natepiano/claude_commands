# Findings Walkthrough (Shared)

This file provides reusable interactive findings review components for commands that present findings to the user with approve/fix/discuss/deny decisions.

## Overview

Commands prepare a `${FINDINGS_LIST}` where each finding has:
- `id` (e.g. F1, F2)
- `title`
- `severity` (critical / important / minor)
- `problem` (full description)
- `impact` (why it matters)
- `recommendation` (actionable suggestion)
- `source` (optional — which expert/agent found it)

Then invoke the shared tagged sections to handle presentation and interaction.

## Usage

Commands reference this file and invoke tagged sections:
- `<FindingsSummaryTable/>` - Present summary table with problem descriptions
- `<FindingsWalkthrough/>` - Walk through each finding, handle responses, track decisions
- `<FindingsCompletion/>` - Final summary, offer plan generation
- `<FindingsKeywords/>` - The approve/fix/discuss/deny keyword block (reusable)
- `<ValidateFindingsResponse/>` - Input validation with accepted variations

## Shared Components

<FindingsSummaryTable>
**Goal:** Present a high-level overview before the interactive walkthrough.

Display a summary table with enough context to understand each finding at a glance:

```
## Findings: ${REVIEW_TOPIC}

${SOURCE_SUMMARY}. Found ${TOTAL} findings.

| #   | Severity | Finding | Problem Summary |
|-----|----------|---------|-----------------|
| F1  | critical | [title] | [1-2 sentence problem description — enough to understand the issue] |
| F2  | important| [title] | [1-2 sentence problem description] |
| ... | ...      | ...     | ...             |

Ready to walk through each finding. You'll be asked to decide on each one.
```

**CRITICAL:** The "Problem Summary" column must explain the actual problem discovered, not just name it. The user should understand what's wrong from this table alone.

Where:
- ${REVIEW_TOPIC} is set by the calling command
- ${SOURCE_SUMMARY} describes who reviewed (e.g. "Reviewed by 4 expert agents", "Reviewed by Codex and Claude", "Codex and Claude perspectives synthesized")
</FindingsSummaryTable>

<FindingsWalkthrough>
**Goal:** Walk through each finding one at a time, collecting user decisions.

Initialize a decisions accumulator: `approved=[], fixed=[], denied=[], discussed=[]`

**For each finding in ${FINDINGS_LIST}:**

1. Present the finding with full detail:

```
## Finding F${N} of ${TOTAL}: ${title}
**Severity:** ${severity}
[If source exists: **Source:** ${source}]

**Problem:**
${full problem description with relevant code/context}

**Impact:**
${why this matters}

**Recommendation:**
${specific actionable suggestion}
```

2. Present action options using <FindingsKeywords/>

3. **STOP and wait for user response.**

4. Handle response using <ValidateFindingsResponse/>:
   - **approve**: Add to approved list with the recommendation. Mark as decided. Proceed to next.
   - **fix**: Implement the recommendation immediately. After fixing, confirm with user, mark as fixed. Proceed to next.
   - **discuss**: Provide deeper analysis. After discussion, re-present <FindingsKeywords/>. Loop until a terminal decision (approve/fix/deny).
   - **deny**: Note the denial. Proceed to next.

5. After each decision, show running tally: "(${approved_count} approved, ${fixed_count} fixed, ${denied_count} denied — ${remaining} remaining)"
</FindingsWalkthrough>

<FindingsKeywords>
## Available Actions
- **approve** - Accept this finding and include in plan
- **fix** - Fix this issue right now before continuing
- **discuss** - Discuss further before deciding
- **deny** - Reject this finding (not a real issue or not worth addressing)

Please select one of the keywords above.
</FindingsKeywords>

<ValidateFindingsResponse>
Handle user response with these accepted variations:

- **approve** (also: "yes", "y", "ok", "accept"): Approve the finding
- **fix** (also: "fix now", "do it"): Fix immediately
- **discuss** (also: "more", "explain", "detail", "why"): Discuss further — after discussion, re-present <FindingsKeywords/> and loop until a terminal decision (approve/fix/deny)
- **deny** (also: "no", "n", "reject", "skip"): Deny the finding
- **Unrecognized input**: Display "Unrecognized response '${input}'. Please select from: approve, fix, discuss, or deny." Re-present <FindingsKeywords/>.
</ValidateFindingsResponse>

<FindingsCompletion>
**Goal:** Summarize all decisions and offer to generate a plan.

1. Display final summary:

```
## Review Complete

**Topic:** ${REVIEW_TOPIC}

| Decision | Count | Findings |
|----------|-------|----------|
| Approved | ${N}  | F1, F3, F5 |
| Fixed    | ${N}  | F2 |
| Denied   | ${N}  | F4 |

### Approved Findings
[For each approved finding, list the title and recommendation]

### Fixed During Review
[For each fixed finding, briefly note what was done]

### Denied
[For each denied finding, list title only]
```

2. If there are approved findings (not yet fixed), ask:

## Next Steps
- **plan** - Generate a plan file from the approved findings
- **done** - End the review session

Please select one of the keywords above.

3. Handle response:
   - **plan**: Create a plan file at `.claude/plans/review-${brief-topic}.md` with the approved findings as implementation tasks, ordered by severity. Include the problem description and recommendation for each.
   - **done**: End the session.
   - If no approved findings remain (all fixed or denied), skip the prompt and end with: "All findings resolved. Review complete."
</FindingsCompletion>
