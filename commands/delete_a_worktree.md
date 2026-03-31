PROTECTED_BRANCHES = main
DEFAULT_REMOTE = origin

I'll discover available worktrees and help you safely delete a worktree and its branch.

First, let me create a todo list to track our progress:

Use TodoWrite tool with todos:
1. content: "Discover and validate worktree options", activeForm: "Discovering and validating worktree options", status: "pending"
2. content: "Get user selection and validate target", activeForm: "Getting user selection and validating target", status: "pending"
3. content: "Check for uncommitted changes", activeForm: "Checking for uncommitted changes", status: "pending"
4. content: "Check for unpushed commits", activeForm: "Checking for unpushed commits", status: "pending"
5. content: "Get final confirmation", activeForm: "Getting final confirmation", status: "pending"
6. content: "Perform worktree and branch deletion", activeForm: "Performing worktree and branch deletion", status: "pending"

Update each todo status to "in_progress" when starting that step, and "completed" when finished.

<ExecutionSteps>
    **EXECUTE THESE STEPS IN ORDER:**

    **STEP 1:** Execute <DiscoverWorktrees/>
    **STEP 2:** Execute <ValidateDeletionTarget/>
    **STEP 3:** Execute <CheckUncommittedChanges/>
    **STEP 4:** Execute <CheckUnpushedCommits/>
    **STEP 5:** Execute <GetFinalConfirmation/>
    **STEP 6:** Execute <PerformDeletion/>
</ExecutionSteps>

<DiscoverWorktrees>
    - Run `bash ~/.claude/scripts/delete_a_worktree/discover_worktrees.sh` to get current context and all worktrees
    - Parse the output to identify deletable worktrees (excluding current one and ${PROTECTED_BRANCHES})
    - Display current worktree and available worktrees for deletion

    ## Worktree Selection
    - **select** - Choose a worktree to delete by entering its number
    - **cancel** - Exit without deleting any worktrees

    Please select one of the keywords above.

    [STOP and wait for user response]
</DiscoverWorktrees>

<ValidateDeletionTarget>
    - Run validation script: `bash ~/.claude/scripts/delete_a_worktree/delete_a_worktree_validation.sh "$SELECTED_WORKTREE"`
    - Parse JSON result to check validation status
    - If status is "error", display the message and STOP
    - Store validation results for later use
</ValidateDeletionTarget>

<CheckUncommittedChanges>
    - Check the validation results from <ValidateDeletionTarget/>
    - If has_uncommitted is true, STOP and inform user they'll be lost
</CheckUncommittedChanges>

<CheckUnpushedCommits>
    - Check the validation results from <ValidateDeletionTarget/>
    - If has_unpushed is true with unpushed_count > 0, WARN user they'll be lost and ask for confirmation
</CheckUnpushedCommits>

<VerifyGitRepository>
    - Handled by discover_worktrees.sh — if not in a git repo, the script reports an error
</VerifyGitRepository>

<GetFinalConfirmation>
    - Display summary of what will be deleted:
      - Worktree path
      - Branch name
      - Whether it has unpushed commits

    ## Confirm Deletion
    - **confirm** - Proceed with deletion of [WORKTREE_PATH] and branch [BRANCH_NAME]
    - **cancel** - Exit without deleting

    Please select one of the keywords above.

    [STOP and wait for user response]
</GetFinalConfirmation>

<PerformDeletion>
    - **IMPORTANT**: Use `dangerouslyDisableSandbox: true` — `git worktree remove` deletes files outside the sandbox's allowed write paths
    - Run `bash ~/.claude/scripts/delete_a_worktree/perform_deletion.sh $SELECTED_WORKTREE $TARGET_BRANCH` to remove the worktree and delete the branch
    - Report success to user
</PerformDeletion>

HAPPY PATH: If all validations pass and user confirms, proceed with deletion
UNHAPPY PATH: Stop and ask user for guidance if:
- Not in a git worktree
- Trying to delete ${PROTECTED_BRANCHES} worktree or branch
- Trying to delete current worktree
- Target worktree has uncommitted changes (unless user acknowledges)
- Target branch has unpushed commits (unless user acknowledges)
- User doesn't explicitly confirm deletion