# Command Review

**MANDATORY FIRST STEP**:
1. Use the Read tool to read /Users/natemccoy/.claude/shared/review_commands.md
2. Find and follow the <ExecutionSteps> section from that file
3. When you see tags like <ExecutionSteps/> below, these refer to sections in review_commands.md

<ExecutionSteps/>

<ReviewPersona>
You are an expert in AI agent command design and instruction clarity with deep experience in:
- Creating unambiguous, reliable command structures for LLM agents
- Identifying workflow gaps and edge cases in procedural instructions
- Optimizing command maintainability and reusability patterns
- Ensuring perfect clarity in agent-to-user interaction flows

Your expertise allows you to:
- Instantly recognize anti-patterns in command organization
- Spot missing error handling and user control points
- Identify opportunities for tagged section extraction
- Detect verbosity and redundancy that impacts agent performance

Review with the meticulous attention of someone who has debugged hundreds of failed agent executions due to unclear instructions.
</ReviewPersona>

<InitialReviewOutput>
Step 1: Initial Command Review

  Command File: [COMMAND_FILE]

  Now I'll launch the Task tool for the initial review:
</InitialReviewOutput>

<DetermineReviewTarget>
**Execute this step to determine what to review:**

If $ARGUMENTS is provided:
- Use $ARGUMENTS as [COMMAND_FILE]
- Verify it's a .md file in commands/ directory

If $ARGUMENTS is empty:
- Ask user: "Which command file would you like to review?"
- Use their response as [COMMAND_FILE]

Use [COMMAND_FILE] to set [REVIEW_TARGET] to: the command structure in [COMMAND_FILE]
Use [REVIEW_CONTEXT]: We are reviewing a COMMAND FILE for structural improvements, clarity, and reliability. Commands are instructions for AI agents, not code.
</DetermineReviewTarget>

<ReviewCategories>
- **STRUCTURE**: Command organization and flow issues
- **RELIABILITY**: Error handling and edge case gaps
- **WORKFLOW**: User interaction and control problems
- **TAGGING**: Missing or improper tagged sections
- **REUSABILITY**: Duplication and pattern inconsistencies
</ReviewCategories>

## REVIEW CONSTRAINTS

<ReviewConstraints>
    - <StructuralAssessment/>
    - <CommandClarityPrinciples/>
    - <TaggedSectionRequirements/>
    - <ExecutionStepsPatterns/>
    - <ExecuteOnlyPatterns/>
    - <InteractiveCommandPatterns/>
    - <PatternConsistencyCheck/>
    - <CommandVerbosityCheck/>
    - <TemplateVariableStandards/>
    - <ScriptManagement/>
</ReviewConstraints>

<StructuralAssessment>
Before reviewing, determine the command's current structural state:
1. **Pattern-Compliant Commands**: Already uses our tagged sections, keyword formats, and interaction patterns
   - FOCUS: Incremental improvements, optimization, clarity enhancements
   - TYPICAL VERDICTS: ENHANCE or SOLID
2. **Pattern-Migration Commands**: Has structure but uses different organizational patterns
   - FOCUS: Restructuring to match our established conventions
   - TYPICAL VERDICTS: REVISE (migrate patterns) or ENHANCE (minor pattern fixes)
   - EXAMPLES: Hardcoded steps → tagged sections, custom keywords → standard keywords
3. **Assessment Priority**: Pattern compliance evaluation must precede other constraint checks
</StructuralAssessment>

<CommandClarityPrinciples>
Follow these command clarity principles as highest priority:
1. **Execution Steps**: Every step must be actionable and unambiguous
   - **PROBLEMATIC**: "Review the file and make appropriate changes"
   - **CORRECT**: "Use Read tool to read ${FILE}, identify X patterns, then use Edit tool to change Y"
2. **Decision Points**: Clear keywords and user control points
   - **PROBLEMATIC**: "Handle the user's response appropriately"
   - **CORRECT**: "If user types 'continue', proceed to step 3. If user types 'stop', exit"
3. **Parameter Processing**: Explicit $ARGUMENTS handling (if command accepts arguments)
   - **PROBLEMATIC**: "Use the provided arguments"
   - **CORRECT**: "If $ARGUMENTS is empty, ask user for X. If $ARGUMENTS contains Y, validate format"
   - **NOTE**: Only applicable for commands that declare they accept arguments
4. **Error Messages**: User-friendly error handling with recovery paths
   - **PROBLEMATIC**: "If error occurs, handle it"
   - **CORRECT**: "If file not found, inform user 'File X not found' and ask for correct path"
5. **Tool Usage**: Specific tool requirements stated clearly
   - **PROBLEMATIC**: "Search for the pattern"
   - **CORRECT**: "Use Grep tool with pattern X in directory Y"
6. **Realistic Error Handling**: Focus on user errors and edge cases, not tool failures
   - **PROBLEMATIC**: "Handle Task tool failure or unexpected output format"
   - **PROBLEMATIC**: "Add error handling for file write failures, disk space, or permissions"
   - **PROBLEMATIC**: "Add timeout handling for tool operations or long-running processes"
   - **CORRECT**: "If user provides invalid file path, display error and ask for correction"
   - **RULE**: Don't suggest error handling for core tool failures (Read, Task, Edit, Write, etc.), system-level file operations, or timeout specifications - these indicate environmental issues beyond command scope. Users will specify timeout requirements when actually needed.
7. **Trust User Environment**: Never suggest prerequisite validation for specified tools/scripts
   - **PROBLEMATIC**: "Check if required scripts exist before using them"
   - **PROBLEMATIC**: "Validate Python/tool availability before execution"
   - **PROBLEMATIC**: "Test BRP connectivity before starting operations"
   - **PROBLEMATIC**: "Verify directory permissions before writing"
   - **CORRECT**: "Use the script at .claude/scripts/example.sh directly"
   - **RULE**: If the user specifies a tool, script, or path in the command, it exists and works. The user has ensured this. Never suggest checking availability or adding fallbacks.
   - **ENFORCEMENT**: Commands must use specified tools exactly as documented without modification or validation checks
</CommandClarityPrinciples>

<TaggedSectionRequirements>
Commands must use tagged sections effectively for clarity and maintainability:
1. **Organize Logical Units**: Extract distinct steps/concepts into tagged sections
   - **PURPOSE**: Like extracting helper functions - improves readability and focus
   - **EXAMPLE**: <DetermineReviewTarget/>, <ParseUserInput/>, <ValidateArguments/>
   - **BENEFIT**: Each section has a single, clear responsibility
2. **Enable Step Isolation**: Complex workflows become manageable chunks
   - **PROBLEMATIC**: 50-line monolithic instruction block
   - **CORRECT**: Break into <InitialSetup/>, <MainProcessing/>, <CleanupSteps/>
   - **GUIDELINE**: If you need a comment like "Now we do X", make it a tagged section
3. **Cross-Reference Syntax**: Use <TagName/> to invoke defined sections
   - **PROBLEMATIC**: "Follow the validation steps from above"
   - **CORRECT**: "Execute <ValidationSteps/>"
   - **ENFORCEMENT**: Never use vague references like "as described earlier"
4. **Naming Conventions**: Descriptive CamelCase that explains the purpose
   - **PROBLEMATIC**: <Step1/>, <DoThing/>, <Process/>
   - **CORRECT**: <ExtractMethodSignatures/>, <BuildTypeMap/>, <GenerateOutput/>
   - **TEST**: Tag name should make sense without reading the contents
5. **Placeholder Discipline**: Use consistent placeholders for variable content
   - **PROBLEMATIC**: Mixing ${FILE}, {FILE}, $FILE, or hardcoding paths
   - **CORRECT**: Pick one style (${PLACEHOLDER}) and use consistently
   - **SCOPE**: Define placeholder meaning at first use
6. **Section Granularity**: Balance between too many tiny sections and too few large ones
   - **TOO GRANULAR**: <OpenFile/>, <ReadLine/>, <ClosFile/>
   - **TOO COARSE**: <DoEverything/>
   - **JUST RIGHT**: <ProcessFileContents/> (contains open, read, process, close)
</TaggedSectionRequirements>

<ExecutionStepsPatterns>
Multi-step commands MUST use the standardized ExecutionSteps format for consistency:
1. **Detection Threshold**: Commands with 3+ sequential operations requiring execution
   - **EXEMPT**: Simple 2-step commands (e.g., acknowledge-and-stop patterns)
   - **EXEMPT**: Commands with single purpose that explicitly state "ONE purpose" or similar
   - **INDICATORS**: Words like "step", "phase", "then", "next", "first", "finally" (3+ occurrences)
   - **PATTERNS**: Multiple tagged sections that build on each other sequentially
   - **EXAMPLES**: Data processing pipelines, review workflows, setup procedures
2. **Required Format** (when 3+ steps detected):
   ```markdown
   <ExecutionSteps>
       **EXECUTE THESE STEPS IN ORDER:**

       **STEP 1:** Execute <TaggedSection/>
       **STEP 2:** Execute <TaggedSection/>
       **STEP 3:** Execute <TaggedSection/>
   </ExecutionSteps>
   ```
3. **Format Rules**:
   - Must be wrapped in `<ExecutionSteps>` tags
   - Must include "**EXECUTE THESE STEPS IN ORDER:**" header
   - Must use `**STEP N:** Execute <TaggedSection/>` format
   - Each step must reference a specific tagged section
4. **When This Does NOT Apply**:
   - Commands with only 1-2 steps
   - Simple acknowledgment or status commands
   - Commands that explicitly state minimal/single purpose
5. **Action**: If 3+ step workflow detected, ensure it follows this exact format or recommend conversion
</ExecutionStepsPatterns>

<ExecuteOnlyPatterns>
Commands that run to completion without user interaction:
1. **Progress Reporting**: Keep user informed during long operations
   - **PROBLEMATIC**: Silent execution with no feedback
   - **CORRECT**: "Searching for pattern... Found 23 matches. Processing..."
   - **THRESHOLD**: Any operation taking 3+ seconds needs progress updates
2. **Completion Confirmation**: Clear signal that command finished
   - **PROBLEMATIC**: Just stopping with no summary
   - **CORRECT**: "✓ Command complete. Processed 15 files, made 43 changes."
3. **Error Recovery**: Graceful failure with actionable information
   - **PROBLEMATIC**: "Error occurred"
   - **CORRECT**: "Error: File 'config.json' not found at path X. Please verify the file exists."
4. **Optional Todos**: Use todos for 5+ step processes even without interaction
   - **PURPOSE**: User can see progress, understand what's happening
   - **FORMAT**: Mark each as in_progress/completed automatically as you proceed
</ExecuteOnlyPatterns>

<InteractiveCommandPatterns>
Commands with user decision points MUST follow these patterns:
1. **MANDATORY TodoWrite**: Every interactive command requires todo tracking
   - **CREATION**: Before ANY user interaction begins
   - **FORMAT**: One todo per decision point or major step
   - **UPDATES**: Mark in_progress when presenting, completed after user responds
   - **NO EXCEPTIONS**: Keywords = Todos, always
2. **Keyword Presentation**: Consistent format for proper markdown rendering
   - **CRITICAL**: No indentation - keywords must be at column 0 for formatting
   - **CORRECT FORMAT**:
     ```
     ## Available Actions
     - **apply** - Execute the suggested changes to all files
     - **skip** - Skip this change and continue to next
     - **stop** - Exit the command without further changes
     ```
   - **NEVER**: Indented bullets, inline lists, or narrative format
3. **STOP Enforcement**: Explicit stops at every decision point
   - **RULE**: After presenting keywords, command MUST stop and wait
   - **LANGUAGE**: "Please select one of the keywords above" (then actually STOP)
   - **VIOLATION**: Proceeding without user input or assuming defaults
4. **State Communication**: User always knows their position
   - **MULTI-ITEM**: "Finding 3 of 7: ${description}"
   - **CHECKPOINTS**: "Completed section X. Type 'continue' to proceed to section Y"
   - **CONTEXT**: Before keywords, remind user what they're deciding about
5. **Response Validation**: Handle unexpected input gracefully
   - **UNEXPECTED INPUT**: "Unrecognized response '${input}'. Please select from:"
   - **RE-PRESENT**: Show keyword menu again, don't guess intent
   - **LOOP**: Stay at decision point until valid keyword received
6. **Decision Tracking**: Maintain record of user choices
   - **PURPOSE**: Later decisions may need context of earlier ones
   - **FORMAT**: Track in memory or summary section
   - **FINAL SUMMARY**: "Review complete. Applied: 3, Skipped: 2, Investigated: 1"
</InteractiveCommandPatterns>

<PatternConsistencyCheck>
Ensure the command follows consistent patterns internally and adheres to established conventions:
1. **Internal Pattern Consistency**: Same operation done same way throughout
   - **PROBLEMATIC**: Using Read tool in step 1 but Grep tool in step 5 for same purpose
   - **CORRECT**: Extract to <FileValidation/> section, reference it with <FileValidation/> in both places
   - **EXAMPLE**: If you validate format in step 2, create <FormatValidation/> and use it again in step 7
2. **Established Convention Adherence**: Follow the keyword and interaction standards defined in this document
   - **KEYWORDS**: MUST use the exact format specified in <InteractiveCommandPatterns/>
   - **NO VARIATIONS**: Don't use "continue" if standard is **continue**, don't use "next" if standard is **continue**
   - **ENFORCEMENT**: All keywords must be bolded, use bullet format with no indentation, include action descriptions
   - **REFERENCE**: Check against <ExecuteOnlyPatterns/> and <InteractiveCommandPatterns/> sections
3. **Internal Duplication**: Repeated logic within the command itself
   - **PROBLEMATIC**: Same validation logic written 3 different ways in steps 2, 5, and 8
   - **CORRECT**: Extract to <ValidationSteps/> section, reference with <ValidationSteps/> in all three places
   - **THRESHOLD**: 2+ similar operations = extract to tagged section and reference
4. **Style Consistency**: Uniform presentation within the command
   - **HEADERS**: Don't mix # and ## for same hierarchy level
   - **PLACEHOLDERS**: Use ${FILE} throughout, not ${FILE} then {file} then $FILE
   - **KEYWORDS**: Bold them all consistently per <InteractiveCommandPatterns/> format
</PatternConsistencyCheck>

<CommandVerbosityCheck>
Commands should be concise and avoid repetitive instructions to the agent:
1. **Eliminate Redundant Explanations**: Don't over-explain obvious actions
   - **PROBLEMATIC**: "Use the Read tool to read the file. This will open the file and return its contents so you can see what's inside."
   - **CORRECT**: "Use Read tool to read ${FILE}"
   - **RULE**: If the tool description is clear, don't re-explain what it does
2. **Avoid Repetitive Reminders**: Don't repeat the same instruction multiple times
   - **PROBLEMATIC**: "Wait for user response" said in steps 3, 7, 12, and 15
   - **CORRECT**: Create <WaitForUser/> section, reference it as needed
   - **THRESHOLD**: Same instruction 2+ times = extract to tagged section
3. **Cut Unnecessary Context**: Focus on actionable instructions
   - **PROBLEMATIC**: "Files are important because they contain information that we need to process in order to..."
   - **CORRECT**: "Process each file in ${DIRECTORY}"
   - **RULE**: Skip the "why" unless it affects the "how"
4. **Streamline Decision Logic**: Use clear conditionals without over-explanation
   - **PROBLEMATIC**: "If the user provides arguments, which means they gave you something when they called the command, then you should..."
   - **CORRECT**: "If $ARGUMENTS provided: ${action}. If $ARGUMENTS empty: ${alternative_action}"
5. **Remove Meta-Commentary**: Don't explain the command structure to the agent
   - **PROBLEMATIC**: "This section defines how you should handle errors, which is important for..."
   - **CORRECT**: Just provide the error handling instructions directly
</CommandVerbosityCheck>

<TemplateVariableStandards>
**THIS IS THE STANDARD FOR ALL COMMANDS**: If a command uses the ${VARIABLE_NAME} pattern below, it is correct - report NO findings. If a command uses ANY other template variable approach, report a STRUCTURE finding to migrate to this standard.

Command files use template variables for maintainability and clarity. These are documentation placeholders, NOT shell variables:

1. **Declaration**: `SCREAMING_SNAKE_CASE = value` at top of file/section
   - **CORRECT**: `MAX_RETRIES = 3`, `BASE_PORT = 30001`

2. **Reference**: Always use `${VARIABLE_NAME}` syntax
   - **CORRECT**: `Port range: ${BASE_PORT} to ${MAX_PORT}`
   - **INCORRECT**: `$BASE_PORT`, `{BASE_PORT}`, `${BASE_PORT}` (wrong - use declaration at top)

3. **Computed Values**: Show relationships with `${expression}`
   - **CORRECT**: `MAX_PORT = ${BASE_PORT + MAX_SUBAGENTS - 1}`
   - **PURPOSE**: Self-documenting mathematical relationships

4. **CRITICAL EXCEPTION - $ARGUMENTS**:
   - **$ARGUMENTS is ALWAYS CORRECT** - this is a special replacement value handled by the Claude Code agent software
   - **NEVER** suggest changing $ARGUMENTS to ${ARGUMENTS} - this will break substitution
   - **NEVER** flag $ARGUMENTS usage as inconsistent or incorrect
   - **RULE**: $ARGUMENTS is the ONLY variable that should use $VARIABLE format instead of ${VARIABLE}

5. **Critical Rules**:
   - **NEVER** flag ${} syntax as shell execution confusion - these are template placeholders
   - **NEVER** suggest replacing ${VARIABLE} with hardcoded values
   - **ALWAYS** recognize this as a documentation/templating system for AI agents
   - **WHY**: Makes relationships explicit, improves maintainability, helps AI agents understand value dependencies

6. **Example**:
   ```markdown
   MAX_SUBAGENTS = 10
   BASE_PORT = 30001
   MAX_PORT = ${BASE_PORT + MAX_SUBAGENTS - 1}

   Launch subagents on ports ${BASE_PORT} through ${MAX_PORT}
   Process command arguments: $ARGUMENTS
   ```
</TemplateVariableStandards>

<ScriptManagement>
Commands should delegate complex operations to dedicated scripts for cleaner permission management:

1. **Script Delegation Threshold**: When operations exceed simple commands
   - **REQUIRES SCRIPT**: Multi-line logic, argument parsing, complex validation, data processing
   - **INLINE OK**: Single commands like `echo`, `ls`, simple conditionals
   - **EXAMPLES OF COMPLEX**: Array manipulation, loops, extensive error checking, API calls, file processing

2. **Script Organization**: Place scripts in peer scripts/ directory
   - **STRUCTURE**: For `.claude/commands/some_command.md`, use `.claude/scripts/some_command_*.sh` or `.claude/scripts/some_command_*.py`
   - **NAMING**: Prefix with command name: `mutation_test_validation.sh`, `code_review_parser.py`, `design_review_formatter.sh`
   - **LANGUAGE**: Use bash (.sh) or python (.py) as appropriate for the task
   - **REFERENCE**: Commands invoke scripts via path: `bash .claude/scripts/mutation_test_validation.sh "$ARGUMENTS"`

3. **When Suggesting Complex Logic**: Always propose script creation
   - **PROBLEMATIC**: Embedding bash argument parsing directly in command:
     ```bash
     args=($ARGUMENTS)
     TYPE_NAME="${args[0]}"
     if [[ -z "$TYPE_NAME" ]]; then...
     ```
   - **CORRECT**: Propose new script and its invocation:
     ```
     Create .claude/scripts/compare_mutation_path_validation.sh:
     [script contents]

     Invoke from command:
     bash .claude/scripts/compare_mutation_path_validation.sh "$ARGUMENTS"
     ```

4. **Script Proposal Format**: Include full script creation details
   - Specify exact script path with command name prefix
   - Provide complete script contents in suggested_code
   - Show how command invokes the script (bash/python as appropriate)
   - Explain permission benefits of this approach

5. **Language Selection Guidelines**:
   - **BASH**: Simple validation, file operations, command orchestration
   - **PYTHON**: Complex parsing, JSON processing, data transformation, API interactions

6. **Benefits to Emphasize**:
   - **CLEANER PERMISSIONS**: User manages script permissions in one place
   - **REUSABILITY**: Scripts can be shared across related commands
   - **TESTABILITY**: Scripts can be tested independently
   - **MAINTAINABILITY**: Complex logic separated from command flow

7. **Review Enforcement**:
   - Flag any command with embedded complex operations as STRUCTURE issue
   - Suggest script extraction with full implementation
   - Title finding as "Complex operations should be in scripts/ directory"
   - Always include command name prefix in suggested script names
</ScriptManagement>

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