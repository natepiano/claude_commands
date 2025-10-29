**IMPORTANT** don't commit the changes that you will examine. Just do the following:

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 1:** Execute <CommitTitleHandling/>
    **STEP 2:** Execute <CommitPrep/>
</ExecutionSteps>

<CommitTitleHandling>
If $ARGUMENTS is provided:
- Use $ARGUMENTS as the commit title (one-liner)
- Proceed directly to <CommitPrep>

If no $ARGUMENTS provided:
- Create TodoWrite with: "Analyze changes and get commit title confirmation"
- Mark todo as in_progress
- Use Task tool with subagent_type="git-agent" to analyze the changes and suggest a commit title
  - Prompt: "First, run 'git status' to verify this is a git repository with uncommitted changes. If not, return an error message. If valid, analyze the current git changes (staged and unstaged) and suggest a concise, conventional commit title (one line, under 72 characters). Return the suggested title with a brief explanation of the changes."
- If git-agent returns an error (not a valid repo or no changes), display the error and stop
- When git-agent returns successfully, present the suggested commit title
- Execute <UserTitleConfirmation/>
- Mark todo as completed after user responds
</CommitTitleHandling>

<UserTitleConfirmation>
Present to user:

## Available Actions
- **use** - Use the suggested commit title
- **change** - Provide a different commit title

Wait for user response.

If user selects **change**: Ask for new title and use their provided title.
If user selects **use**: Use the suggested title.

After user responds, proceed to next step.
</UserTitleConfirmation>

<CommitPrep>
- Create TodoWrite with: "Generate full commit message"
- Mark todo as in_progress
- Use Task tool with subagent_type="git-agent" to generate the full commit message
  - Prompt: "Generate a concise conventional commit message for the current git changes. The commit title should be: '[established commit title]'. Create a commit body (10-15 lines) with high-level bullet points covering: what changed, why the change was made, and key impact. Avoid exhaustive details or deep subsections. Follow conventional commit format."
- When git-agent returns with the full commit message, show it to the user:

```
**Proposed commit message:**
[full commit message from git-agent]
```

- Mark todo as completed
- STOP and execute <FinalCommitDecision/>
</CommitPrep>

<FinalCommitDecision>
Present to user:

## Available Actions
- **commit** - Execute the git commit with the prepared message
- **abandon** - Stop without committing

Wait for user response.

If user selects **commit**:
- Use Task tool with subagent_type="git-agent" to stage and commit the changes
  - Prompt: "Stage all changes with 'git add' and commit with the following message: '[full commit message]'. After committing, return the commit hash and changes summary from the git commit output."
- When git-agent returns, execute <CommitOutput/> with the returned information

If user selects **abandon**: Run `git reset` to unstage any changes (if staged) and stop

After user responds, execute their choice.
</FinalCommitDecision>

<CommitOutput>
Using the commit information returned by git-agent, format output as:

```
✅ **Commit successful**

**Commit hash**: `[short hash from git-agent]`
**Changes**: [files changed summary from git-agent]

[additional git status info from git-agent]
```

**Formatting requirements**:
- Each field on its own line
- Commit hash in code backticks
- Blank line between commit info and additional status
- Use commit information provided by git-agent
</CommitOutput>
