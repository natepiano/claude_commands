<Persona>
@~/.claude/shared/personas/git_expert_persona.md
</Persona>

**IMPORTANT** don't commit the changes that you will examine. Just do the following:

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    **STEP 0:** Execute <Persona/> to adopt the Git Expert persona
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
  - Prompt: "Analyze the current git changes (staged and unstaged) and suggest a concise, conventional commit title (one line, under 72 characters). Return only the suggested title with a brief explanation of the changes."
- When git-agent returns, present the suggested commit title
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
- Create TodoWrite with: "Generate full commit message and stage changes"
- Mark todo as in_progress
- run `git status` to ensure you're within a git repository that has uncommitted changes
- Use Task tool with subagent_type="git-agent" to generate the full commit message
  - Prompt: "Generate a complete conventional commit message for the current git changes. The commit title should be: '[established commit title]'. Create a detailed commit body that explains what changed and why. Follow conventional commit format."
- When git-agent returns with the full commit message, stage the changes with `git add`
- Show the user the staged changes and proposed commit message in this format:

```
**Staged changes:**
[git diff --staged output]

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
- **abandon** - Unstage all changes and stop without committing

Wait for user response.

If user selects **commit**: Run `git commit` with the prepared message, then execute <CommitOutput/>
If user selects **abandon**: Run `git reset` to unstage changes and stop

After user responds, execute their choice.
</FinalCommitDecision>

<CommitOutput>
After successful commit, format output as:

```
✅ **Commit successful**

**Commit hash**: `[short hash]`
**Changes**: [files changed summary]

[additional git status info]
```

**Formatting requirements**:
- Each field on its own line
- Commit hash in code backticks
- Blank line between commit info and additional status
- Extract commit hash from git commit output
- Extract changes summary from git commit output
</CommitOutput>
