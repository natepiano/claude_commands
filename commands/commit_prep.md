**IMPORTANT** don't commit the changes that you will examine. Just do the following:

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Execute <AnalyzeChanges/>
    **STEP 2:** Execute <CommitTitleHandling/>
    **STEP 3:** Execute <GenerateCommitBody/>
    **STEP 4:** Execute <FinalCommitDecision/>
</ExecutionSteps>

<AnalyzeChanges>
Run `git status` and `git diff` (staged and unstaged) to understand the current changes.
**CRITICAL**: You must evaluate EVERY uncommitted file for inclusion in the commit — regardless of file type (.md, .yml, .toml, .rs, etc.) and regardless of what task you were working on prior to this command. Do not carry over any file exclusions from previous operations. The commit candidate set is determined solely by `git status`, not by what you were previously focused on.
If no uncommitted changes exist, inform the user and stop.
</AnalyzeChanges>

<CommitTitleHandling>
If $ARGUMENTS is provided:
- Use $ARGUMENTS as the commit title

If no $ARGUMENTS provided:
- Suggest a concise conventional commit title (one line, under 72 characters)
- Execute <UserTitleConfirmation/>
</CommitTitleHandling>

<UserTitleConfirmation>
Present to user:

## Available Actions
- **use** - Use the suggested commit title
- **change** - Provide a different commit title

Wait for user response.

If user selects **change**: Ask for new title and use their provided title.
If user selects **use**: Use the suggested title.
</UserTitleConfirmation>

<GenerateCommitBody>
Using the analyzed changes and established commit title, generate a full conventional commit message:
- Commit body: 10-15 lines of high-level bullet points covering what changed, why, and what it affects
- Do not end with flowery summary statements that editorialize the change (e.g., "This improves maintainability and makes the codebase cleaner") — just state the facts
- Avoid exhaustive details or deep subsections

Present the full commit message to the user:

```
**Proposed commit message:**
[full commit message]
```
</GenerateCommitBody>

<FinalCommitDecision>
Present to user:

## Available Actions
- **commit** - Execute the git commit with the prepared message
- **abandon** - Stop without committing

Wait for user response.

If user selects **commit**:
- Stage all changes with `git add` and commit with the prepared message
- Execute <CommitOutput/>

If user selects **abandon**: Run `git reset` to unstage any changes (if staged) and stop
</FinalCommitDecision>

<CommitOutput>
Format output as:

```
**Commit successful**

**Commit hash**: `[short hash]`
**Changes**: [files changed summary]
```

**Formatting requirements**:
- Each field on its own line
- Commit hash in code backticks
- Blank line between commit info and additional status
</CommitOutput>
