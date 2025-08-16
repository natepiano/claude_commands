# Design Info Command

This command provides customized instructions for the next design review.

## Purpose
When the user runs this command, they are giving ME (Claude) specific customized instructions that I must incorporate when they subsequently issue the design_review command.

## Instructions for Claude

When this command is executed:

1. **Receive Custom Instructions**: The <Customization> section below contains specific instructions from the user for how to conduct the upcoming design review:

<Customization>
$ARGUMENTS
</Customization>

2. **Store Instructions**: I will remember these customized instructions and use them to guide the Task subagent when the user issues the design_review command.

3. **Acknowledge Understanding**: I will respond with ONLY the word "Acknowledged" to signal that I understand the custom instructions.

4. **Wait for design_review Command**: After acknowledging, I will STOP and wait for the user to issue the design_review command.

## When design_review is Executed

When the user subsequently issues the design_review command, I will:
1. Include the customized instructions from <Customization/> in the context provided to the Task subagent
2. Ensure the subagent prioritizes these custom goals alongside standard review criteria
3. Make sure the custom instructions guide the specific focus areas of the review

## Important Notes
- The customized instructions in <Customization/> take precedence where they conflict with standard review criteria
- These instructions represent the user's specific goals for this particular design review
- The "Acknowledged" response is a clear signal that I'm ready to incorporate these instructions
