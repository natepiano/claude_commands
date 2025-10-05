# Command Review

**MANDATORY FIRST STEP**:
1. Shared review commands: @~/.claude/shared/review_commands.md
2. That file provides the required <ExecutionSteps> for this command
3. Some tagged sections reference review_commands.md (e.g., <ExecutionSteps/>), others are defined in this file (e.g., <ReviewPersona/>)

<ReviewConfiguration>
MAX_FOLLOWUP_REVIEWS = 8
CONSTRAINTS_FILE = @~/.claude/shared/constraints/command_review_constraints.md

</ReviewConfiguration>

<ExecutionSteps/>

<ReviewPersona>
@~/.claude/shared/personas/ai_command_expert_persona.md
</ReviewPersona>

<InitialReviewOutput>
**Step 1**: Initial Command Review
**Command File**: ${COMMAND_FILE}
**Max Followup Reviews**: ${MAX_FOLLOWUP_REVIEWS}
Now I'll launch the Task tool for the initial command review:
</InitialReviewOutput>

<DetermineReviewTarget>
**Execute this step to determine what to review:**

COMMAND_FILE = command file path determined from $ARGUMENTS or user input
REVIEW_TARGET = the command structure in ${COMMAND_FILE}
REVIEW_CONTEXT = We are reviewing a COMMAND FILE for structural improvements, clarity, and reliability. Commands are instructions for AI agents, not code.

If $ARGUMENTS is provided:
- Set COMMAND_FILE = $ARGUMENTS
- Verify ${COMMAND_FILE} is a .md file in commands/ directory

If $ARGUMENTS is empty:
- Ask user: "Which command file would you like to review?"
- Set COMMAND_FILE = user's response

REVIEW_TARGET is set to: the command structure in ${COMMAND_FILE}
REVIEW_CONTEXT is set to: We are reviewing a COMMAND FILE for structural improvements, clarity, and reliability. Commands are instructions for AI agents, not code.
</DetermineReviewTarget>

<ReviewCategories>
- **STRUCTURE**: Command organization and flow issues
- **RELIABILITY**: Error handling and edge case gaps
- **WORKFLOW**: User interaction and control problems
- **TAGGING**: Missing or improper tagged sections
- **REUSABILITY**: Duplication and pattern inconsistencies
</ReviewCategories>

## REVIEW CONSTRAINTS

Review constraints are defined in: ${CONSTRAINTS_FILE}

The constraints file is used by both initial review and investigation phases.
The main difference is how each phase handles validation failures:
- **Initial Review**: Discard findings that fail validation
- **Investigation**: Use SOLID verdict for findings that fail validation

<ReviewKeywords>
    **For ENHANCE verdicts:**
    - improve: Apply the suggested improvements to the command
    - skip: Skip this improvement and continue
    - investigate: Launch deeper investigation

    **For REVISE verdicts:**
    - agree: Apply the revised improvements
    - skip: Skip this improvement and continue
    - investigate: Launch deeper investigation

    **For SOLID verdicts (finding incorrect, command is fine):**
    - accept: Accept that the command is well-structured (default)
    - override: Apply the improvement despite the recommendation
    - investigate: Launch investigation to reconsider
</ReviewKeywords>

<ReviewFollowupParameters>
    When using ReviewFollowup from review_commands.md, substitute:
    - [EXPECTED_VERDICTS]: ENHANCE, REVISE, or SOLID
</ReviewFollowupParameters>

<KeywordExecution>
    **improve**: Use Edit tool to apply the suggested improvements to the command file specified in location
    **agree**: Use Edit tool to apply the revised improvements to the command file specified in location
    **skip**: Mark as skipped and continue (maintain list for final summary)
    **accept**: Mark as accepted (agreeing with SOLID verdict) and continue
    **override**: Use Edit tool to apply the improvements despite SOLID verdict
    **investigate**: Ask user "What specific aspect would you like me to investigate?", then launch Task tool with their focus
</KeywordExecution>
