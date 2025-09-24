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
- First run `git status` and `git diff` to understand the changes
- Suggest a commit header based on the changes
- Execute <UserTitleConfirmation/>
- Mark todo as completed after user responds
</CommitTitleHandling>

<UserTitleConfirmation>
Present to user:

## Available Actions
- **confirm** - Use the suggested commit title
- **change** - Provide a different commit title

Wait for user response.

If user selects **change**: Ask for new title and use their provided title.
If user selects **confirm**: Use the suggested title.

After user responds, proceed to next step.
</UserTitleConfirmation>

<CommitPrep>
- run `git status` to ensure you're within a git repository that has uncommitted changes
- create a full commit message using the established commit title
- stage the changes with `git add`
- STOP - do not run `git commit`
- Show the user the staged changes and proposed commit message
</CommitPrep>

**CRITICAL** Never run `git commit` - only prepare and show the commit message to the user
