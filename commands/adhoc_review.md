# Adhoc Review

Conduct an interactive review of issues or topics by organizing them into a todo list and walking through each item with user discussion and approval.

**Arguments**: $ARGUMENTS (optional description of review context or specific topics to include)

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 1:** Execute <ReviewSetup/>
    **STEP 2:** Execute <TodoCreation/>
    **STEP 3:** Execute <InteractiveReview/>
    **STEP 4:** Execute <ReviewCompletion/>
</ExecutionSteps>

## STEP 1: SETUP

<ReviewSetup>
    **Initialize Review Context:**

    1. If $ARGUMENTS provided, use as review context
    2. Otherwise, analyze current context for potential review items:
       - Look for recent error messages, findings, or issues discussed
       - Check for lists, todos, or action items mentioned
       - Identify any documents or code that was being analyzed
    3. If context suggests clear review items:
       - Ask user: "Based on our discussion, it looks like you want to review [identified items]. Should I proceed with these, or would you like to specify different items?"
       - Use user's confirmation or alternative specification
    4. If no clear context:
       - Ask user: "What would you like to review? (topics, issues, findings, etc.)"
    5. Gather all items to be reviewed and prepare for todo list creation
</ReviewSetup>

## STEP 2: TODO CREATION

<TodoCreation>
    **Build Interactive Todo List:**

    1. Create todo list using TodoWrite tool with all identified review items
    2. Each todo should be:
       - content: Clear, actionable description of the item to review
       - activeForm: Present continuous form ("Reviewing [item]")
       - status: "pending"
    3. Display the created todo list to user
    4. Ask: "Ready to begin walking through these items one by one?"
    5. Wait for user confirmation before proceeding
</TodoCreation>

## STEP 3: INTERACTIVE REVIEW

<InteractiveReview>
    **Walk Through Each Item:**

    For each todo item:

    1. Update current item to "in_progress" status
    2. Present the item clearly with progress indicator:
       - **Reviewing item [current_number] of [total_items]**
       - **Item**: [todo content]
       - **Context**: [relevant background or details]
       - **Action Type**: [If actionable: describe action | If discussion: "Discussion only"]
       - **Discussion Points**: [key aspects to consider]
    3. Facilitate discussion:
       - Present relevant information
       - Ask clarifying questions if needed
       - Summarize key points
    4. Present action options:

       ## Available Actions
       - **continue** - Proceed with this item (take action if applicable)
       - **skip** - Skip without discussion/action
       - **discuss** - Further discuss this item before deciding
       - **stop** - Exit the review process

       Please select one of the keywords above.
    5. Wait for user keyword response
    6. Execute based on keyword:
       - If **continue**: Execute any applicable action, mark completed, proceed to next
       - If **skip**: Mark as skipped, proceed to next
       - If **discuss**: Continue discussion until user provides continue/skip/stop
       - If **stop**: Exit review process
    7. Update todo to "completed" with outcome noted
    8. Automatically proceed to next item (no second prompt needed)

    **CRITICAL**: Each item requires explicit user decision via keyword
</InteractiveReview>

## STEP 4: COMPLETION

<ReviewCompletion>
    **Wrap Up Review Session:**

    1. Display final todo list showing all completed items
    2. Provide summary:
       - Total items reviewed: [count]
       - Actions taken: [brief list]
       - Key decisions made: [summary]
    3. Ask: "Is this review session complete, or are there additional items to discuss?"
    4. Handle any additional items or conclude session
</ReviewCompletion>
