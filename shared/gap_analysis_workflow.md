# Gap Analysis Workflow (Shared)

This file provides reusable gap analysis components for commands that need to perform deep implementation completeness checks.

## Overview

Gap analysis identifies missing or incomplete implementation details in plan documents by:
1. Launching a Task tool with deep analysis prompt
2. Parsing returned gaps with severity ratings
3. Presenting gaps interactively for user review
4. Allowing fix/skip/investigate decisions per gap

## Usage

Commands reference this file and invoke tagged sections:
- `<GapAnalysisPrompt/>` - Deep analysis Task prompt
- `<GapReview/>` - Interactive review loop
- `<GapOutput/>` - Standardized presentation format

## Shared Components

<GapAnalysisPrompt>
Think harder about whether this implementation plan is actually complete.
Read actual current code for each file to be modified.

Check for:
1. Vague changes without concrete implementation ("refactor X" without HOW)
2. Missing function signatures, error handling, edge cases
3. Dependencies/integrations the plan doesn't mention
4. Code that will break but isn't updated

For each gap, provide: gap_type, file, current_code, plan_proposal, what's_missing, severity (CRITICAL/HIGH/MEDIUM/LOW)

Return JSON:
{
  "gaps_found": boolean,
  "gap_count": number,
  "gaps": [
    {
      "gap_type": "string",
      "file": "path/to/file",
      "current_code": "code snippet",
      "plan_proposal": "what plan says",
      "what's_missing": "specific details",
      "severity": "CRITICAL|HIGH|MEDIUM|LOW"
    }
  ],
  "summary": "assessment"
}
</GapAnalysisPrompt>

<GapReview>
    **Interactive Gap Review - One at a Time**

    Use TodoWrite tool to create tracking for each gap:
    - Create todos for each gap: "Review gap: ${gap_type} in ${file}"
    - Status: "pending" for all initially

    For each gap (in the order they appear in the gaps array):

    1. Mark current gap todo as "in_progress"
    2. Present gap using <GapOutput/> format
    3. Include counter: "(Gap ${current_number} of ${total_gaps})"
    4. Display keywords at column 0:

## Available Actions
- **fix** - Address this gap in the plan before continuing
- **skip** - Accept this gap and continue anyway
- **investigate** - Launch deeper investigation of the gap
- **stop** - Cancel the process

    5. STOP and wait for user's keyword response
    6. Handle keyword response:

       **If user says "fix":**
       a. Display: "Fixing gap in the plan..."
       b. Use Read tool to read the plan document from ${PLAN_DOCUMENT}
       c. Analyze the gap and determine what needs to be added/clarified in the plan
       d. Use Edit tool to update the plan document with the missing details:
          - Add missing function signatures with complete before/after examples
          - Clarify vague instructions with concrete implementation steps
          - Add missing edge case handling or error handling details
          - Include structural references or code snippets
          - Ensure the fix addresses the "what's_missing" from the gap
       e. Display: "✅ Gap fixed in ${PLAN_DOCUMENT}"
       f. Ask: "Ready to continue to the next gap?"
       g. Display keywords:
          - **continue** - Move to next gap
          - **stop** - Cancel the process
       h. STOP and wait for user response
       i. If user says "continue", mark gap as completed and continue to next gap
       j. If user says "stop", exit process
       k. Execute <ValidateUserResponse/> with expected_keywords: [continue, stop]

       **If user says "skip":**
       a. Display: "Skipping this gap - accepting it as-is"
       b. Mark gap as skipped
       c. Mark gap todo as completed
       d. Continue to next gap

       **If user says "investigate":**
       a. Display: "🔍 Launching deeper investigation..."
       b. Use Task tool with general-purpose subagent:
          - description: "Investigate gap: ${gap_type}"
          - prompt: "Analyze this implementation gap in detail:
                     File: ${file}
                     Gap type: ${gap_type}
                     What plan says: ${plan_proposal}
                     What's missing: ${what's_missing}
                     Current code: ${current_code}

                     Provide:
                     1. Root cause analysis of why this gap exists
                     2. Specific code snippets needed to fill the gap
                     3. Recommended fix with exact text to add to plan
                     4. Verification steps to confirm fix is complete"
       c. Display the investigation results
       d. Re-present the same gap with keywords (user can now choose fix, skip, investigate again, or stop)
       e. STOP and wait for user response
       f. Return to step 6 to handle the new keyword

       **If user says "stop":**
       a. Display: "Gap review cancelled by user"
       b. Exit process

    7. Execute <ValidateUserResponse/> for initial response with:
       expected_keywords: [fix, skip, investigate, stop]
       option_descriptions: [
           "- **fix** - Address this gap in the plan before continuing",
           "- **skip** - Accept this gap and continue anyway",
           "- **investigate** - Launch deeper investigation of the gap",
           "- **stop** - Cancel the process"
       ]

    After all gaps reviewed, display summary:
    ```
    Gap Review Complete
    -------------------
    Total gaps: ${total_count}
    Fixed: ${fix_count}
    Skipped: ${skip_count}
    ```
</GapReview>

<GapOutput>
# Gap ${current_number} of ${total_gaps}: ${gap_type}
**Severity**: ${severity}
**File**: ${file}

## What the plan says:
${plan_proposal}

## What's actually needed:
${what's_missing}

## Current code context:
```
${current_code}
```

## Why this matters:
[Explain the compilation/runtime impact if this gap isn't addressed]
</GapOutput>

<ValidateUserResponse>
    # Parameters: expected_keywords (array), option_descriptions (array)
    If response is not one of expected_keywords:
        Display: "Unrecognized response '[user_input]'. Please select from:"
        For each option in option_descriptions:
            Display: option
        STOP and wait for valid input
</ValidateUserResponse>
