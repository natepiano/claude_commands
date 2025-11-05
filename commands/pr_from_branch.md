**Arguments**: None (uses current branch)

<Persona>
@~/.claude/shared/personas/git_expert_persona.md
</Persona>

I'll create a PR from your current branch and manage the merge workflow.

First, let me create a todo list to track our progress:

Use TodoWrite tool with todos:
1. content: "Verify branch and generate PR message", activeForm: "Verifying branch and generating PR message", status: "pending"
2. content: "Create pull request", activeForm: "Creating pull request", status: "pending"
3. content: "Wait for merge confirmation", activeForm: "Waiting for merge confirmation", status: "pending"
4. content: "Switch to main and pull", activeForm: "Switching to main and pulling", status: "pending"
5. content: "Clean up branch if requested", activeForm: "Cleaning up branch if requested", status: "pending"

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 0:** Execute <Persona/> to adopt the Git Expert persona
    **STEP 1:** Execute <VerifyAndGeneratePRMessage/>
    **STEP 2:** Execute <CreatePullRequest/>
    **STEP 3:** Execute <WaitForMerge/>
    **STEP 4:** Execute <SwitchToMainAndPull/>
    **STEP 5:** Execute <CleanupBranch/>
</ExecutionSteps>

<VerifyAndGeneratePRMessage>
    - Use Bash tool to run `git branch --show-current` to get current branch name
    - If on main/master, inform user "You are on the main branch. Please switch to a feature branch first." and exit
    - Store branch name for later use
    - Use Bash tool to run `git log --pretty=format:'%h %s' origin/main..HEAD` to get commit list
    - Use Bash tool to run `git diff --stat origin/main..HEAD` to get change summary
    - Analyze commits and create PR message with format:
      * Title: Single line summary of the overall change
      * Body: Bullet points of key changes based on commits
      * Include diff stats if significant
    - **CRITICAL**: Do not include any references to Claude in the message
    - Ensure message is concise and focuses on user-facing changes
    - Display the generated message to user
</VerifyAndGeneratePRMessage>

<CreatePullRequest>
    Display the generated PR message and ask:

    ## PR Message Review
    - **approve** - Create PR with this message
    - **edit** - Modify the PR message before creating
    - **cancel** - Exit without creating PR

    Please select one of the keywords above.

    [STOP and wait for user response]

    If user chooses **edit**: Ask for their modified message and use it
    If user chooses **approve**: Use the generated message
    If user chooses **cancel**: Exit without creating PR

    After user provides approval or edited message:
    - Push current branch to remote if not already pushed
    - Create the pull request using `gh pr create` with the approved message
    - Display PR URL to user
</CreatePullRequest>

<WaitForMerge>
    Inform user: "PR has been created successfully."

    ## Merge Status Check
    - **merged** - PR has been merged into main, proceed with cleanup
    - **wait** - PR is still pending, check back later

    Please select one of the keywords above when the PR is ready.

    [STOP and wait for user response]

    Continue only when user confirms merge is complete.
</WaitForMerge>

<SwitchToMainAndPull>
    - Use Bash tool to run `git checkout main` to switch to main branch
    - Run `git pull` to get the merged changes
    - Confirm successful pull operation
    - Display updated main branch status
</SwitchToMainAndPull>

<CleanupBranch>
    Ask user about branch cleanup:

    ## Branch Cleanup
    - **cleanup** - Delete the local and remote branch (changes are now in main)
    - **keep** - Keep the branch for future work

    Please select one of the keywords above.

    [STOP and wait for user response]

    If user chooses **cleanup**:
    - Run `git branch -d [branch-name]` to delete local branch
    - Run `git push origin --delete [branch-name]` to delete remote branch
    - Confirm successful removal

    If user chooses **keep**:
    - Inform user the branch remains available for future work
    - STOP (no further action needed)
</CleanupBranch>
