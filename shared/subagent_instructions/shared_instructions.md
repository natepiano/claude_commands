# Shared Review Subagent Instructions

This file contains universal review behavior used by all review types (design, code, command).

## How to Use These Instructions

**Check the Phase variable in your prompt:**
- If Phase = INITIAL_REVIEW: Follow <InitialReviewWorkflow/> in your review-type-specific file
- If Phase = INVESTIGATION: Follow <InvestigationWorkflow/> in your review-type-specific file

**Then use these shared sections as referenced:**

## Shared Components

<InvestigationVerdictSelection>
**CRITICAL FOR VERDICT SELECTION:**

- Use REJECTED/FIX NOT RECOMMENDED/SOLID when:
  * Original finding is wrong AND no changes are needed, OR
  * **Plan already correctly addresses this issue (redundant finding)**

- Use MODIFIED/FIX MODIFIED/REVISE when:
  * Original finding is wrong OR incomplete BUT investigation reveals different issues that DO need fixing

- Use CONFIRMED/FIX RECOMMENDED/ENHANCE when:
  * Original finding is correct AND plan does not already address it

- **If you discover any issues during investigation (even if the original finding misdiagnosed them), you MUST use a verdict that recommends action (CONFIRMED/MODIFIED/etc.), NOT a rejection verdict**

- For MODIFIED verdicts: Rewrite the issue description and suggested_code to address the newly discovered problems, not the original finding's incorrect diagnosis
</InvestigationVerdictSelection>

<CodeExtractionRequirements>
When reporting code issues, you MUST provide enough context to understand the problem:
- Include at least 5-10 lines of surrounding code (more if needed for complex logic)
- Show the complete function/method containing the issue when possible
- Include relevant imports, type definitions, or related structures
- A single line of code is NEVER sufficient - provide the full context
</CodeExtractionRequirements>

<IDGenerationRules>
When generating IDs for review findings:
1. FIRST, scan the entire plan document for existing review IDs (e.g., DESIGN-1, TYPE-SYSTEM-3)
2. Track the highest number used for each category
3. Generate NEW IDs starting from the next available number
4. Each category tracks its own sequence independently
5. NEVER reuse existing ID numbers - this causes confusion across multiple review passes
6. Even if the original finding was rejected, do not reuse its number

Example: If the plan already contains DESIGN-1 through DESIGN-7, start your new findings at DESIGN-8
</IDGenerationRules>

<JsonOutputFormat>
**CRITICAL**: Format your response message as JSON text for the main agent to parse. Do NOT create, write, or save any files. Do NOT use the Write, Edit, or any file creation tools.

Include the JSON structure directly in your response message text. The main agent will extract and parse this JSON from your message response.

**RESPONSE FORMAT**: Your message response must contain ONLY the JSON object as text, with no markdown code blocks, no additional narrative, and no file operations.

**Base JSON Structure for Review Findings:**
```json
{
  "id": "${CATEGORY}-${NUMBER}",
  "category": "${CATEGORY}",
  "title": "[Brief descriptive title]",
  "redundancy_check": {
    "grep_performed": true,
    "plan_addresses_this": "YES_IDENTICAL" | "YES_DIFFERENT" | "NO",
    "plan_section": "[Section title if found, or 'NOT FOUND']",
    "plan_solution": "[What the plan proposes, or 'Plan does not address this']",
    "assessment": "REDUNDANT" | "ALTERNATIVE_NEEDED" | "GAP"
  },
  "location": {
    "plan_reference": "[If reviewing a plan: section title - e.g., 'Section: Mutation Path Implementation']",
    "code_file": "[Relative path to actual code file from project root]",
    "line_start": [number in code file],
    "function": "[Function/method name if applicable]",
  },
  "issue": "[Specific problem description]",
  "current_code": "[Code snippet or text showing the issue]",
  "suggested_code": "[Improved version or recommendation]",
  "impact": "[Why this matters]"
}
```

**Base Requirements:**
- ${CATEGORY} must be from <ReviewCategories/> (e.g., TYPE-SYSTEM, QUALITY, DESIGN)
- ${NUMBER} must follow <IDGenerationRules/>
- TYPE-SYSTEM issues should be sorted first in findings array
- For plan/design reviews: location MUST include both plan_reference AND code_file
- current_code must be from the ACTUAL code file, not copied from the plan
- If code file cannot be identified, set location.code_file to "UNKNOWN - NEEDS INVESTIGATION"
- current_code must follow <CodeExtractionRequirements/>
- current_code must be PURE CODE ONLY - no markdown headers, instructions, or prose

**Redundancy Check Field (MANDATORY for Initial Review):**
- **grep_performed**: Must be true - confirms you searched the plan document
- **plan_addresses_this**:
  * "YES_IDENTICAL": Plan proposes the exact same solution you're suggesting
  * "YES_DIFFERENT": Plan addresses this issue but with a different approach
  * "NO": Plan does not mention this issue at all
- **plan_section**: The section title where the plan discusses this (e.g., "Phase 1a: Update VariantName")
- **plan_solution**: Brief summary of what the plan proposes
- **assessment**:
  * "REDUNDANT": Plan already has the correct fix → DO NOT include this finding
  * "ALTERNATIVE_NEEDED": Plan's approach is wrong/incomplete, your alternative is better → Include finding
  * "GAP": Plan doesn't address this at all → Include finding

**CRITICAL**: If assessment = "REDUNDANT", you MUST discard this finding entirely. Do not include it in the findings array.
</JsonOutputFormat>

<InvestigationRestrictions>
**CRITICAL RESTRICTIONS - You may ONLY:**
- Read files to understand context
- Analyze code structure and patterns
- Evaluate the proposed change

**You may NOT:**
- Run any applications or servers (cargo run, npm start, etc.)
- Execute blocking or interactive commands
- Create or modify any files
- Run tests or build commands
- Make any changes to the codebase
</InvestigationRestrictions>

<ReasoningGuidelines>
Include detailed reasoning for your verdict in simple, easy-to-understand terms:
- Avoid technical jargon where possible
- Explain the "why" in plain language
- Focus on practical impact rather than theoretical concepts

**CRITICAL FOR REJECTED VERDICTS**: You MUST clearly explain:
- What the current plan/code does
- What the finding incorrectly suggested
- Why the current approach is actually correct AND why no alternative changes are needed
- Use the format "The finding is incorrect because..." to be explicit
- Remember: REJECTED means NO changes needed at all - if ANY issue exists, use MODIFIED instead

**CRITICAL FOR ALL VERDICTS**: Structure your reasoning to clearly separate:
- What problem the finding identified
- What the current code/plan actually does
- What change is being proposed
- Why you agree/disagree/modify the proposal
- Use plain language that a developer can quickly understand
</ReasoningGuidelines>
