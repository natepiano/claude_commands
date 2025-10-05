**Arguments**: $ARGUMENTS (worktree folder name)

<Persona>
@~/.claude/shared/personas/git_expert_persona.md
</Persona>

I'll create a PR from your worktree and manage the merge workflow.

First, let me create a todo list to track our progress:

Use TodoWrite tool with todos:
1. content: "Generate PR message from commits", activeForm: "Generating PR message from commits", status: "pending"
2. content: "Create pull request", activeForm: "Creating pull request", status: "pending"
3. content: "Wait for merge confirmation", activeForm: "Waiting for merge confirmation", status: "pending"
4. content: "Switch to main worktree and pull", activeForm: "Switching to main worktree and pulling", status: "pending"
5. content: "Clean up worktree if requested", activeForm: "Cleaning up worktree if requested", status: "pending"

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 0:** Execute <Persona/> to adopt the Git Expert persona
    **STEP 1:** Execute <GeneratePRMessage/>
    **STEP 2:** Execute <CreatePullRequest/>
    **STEP 3:** Execute <WaitForMerge/>
    **STEP 4:** Execute <SwitchToMainAndPull/>
    **STEP 5:** Execute <CleanupWorktree/>
</ExecutionSteps>

<GeneratePRMessage>
    - Validate that $ARGUMENTS is provided (worktree folder name)
    - If $ARGUMENTS is empty, inform user "Please provide worktree folder name" and exit
    - Use Bash tool to run `git log --pretty=format:'%h %s' origin/main..HEAD` to get commit list
    - Use Bash tool to run `git diff --stat origin/main..HEAD` to get change summary
    - Analyze commits and create PR message with format:
      * Title: Single line summary of the overall change
      * Body: Bullet points of key changes based on commits
      * Include diff stats if significant
    - **CRITICAL**: Do not include any references to Claude in the message
    - Ensure message is concise and focuses on user-facing changes
    - Display the generated message to user
</GeneratePRMessage>

<CreatePullRequest>
    Display the generated PR message and ask:

    ## PR Message Review
    - **approve** - Create PR with this message
    - **edit** - Modify the PR message before creating
    - **cancel** - Exit without creating PR

    Please select one of the keywords above.

    [STOP and wait for user response]

    If approved, create the pull request using appropriate git/gh commands.
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
    - Use Bash tool to run `git worktree list` to display all worktrees
    - Identify the main worktree (typically the first entry or one without a branch name in parentheses)
    - Use `cd` command to switch to that worktree's absolute path
    - If multiple worktrees exist, the main worktree is usually the one without branch info or marked as the primary checkout
    - Run `git pull` to get the merged changes
    - Confirm successful pull operation
</SwitchToMainAndPull>

<CleanupWorktree>
    Ask user about worktree cleanup:

    ## Worktree Cleanup
    - **cleanup** - Remove the worktree folder $ARGUMENTS (changes are now in main)
    - **keep** - Keep the worktree for future work

    Please select one of the keywords above.

    [STOP and wait for user response]

    If user chooses cleanup:
    - Run `cd ..` to move out of worktree directory
    - Run `git worktree remove $ARGUMENTS` to remove the worktree
    - Confirm successful removal

    If user chooses keep:
    - Inform user the worktree remains available for future work
    - STOP (no further action needed)
</CleanupWorktree>
