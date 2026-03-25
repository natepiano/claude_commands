Use TodoWrite tool to create initial todos:
- "Suggest worktree and branch name to user"
- "Get user approval for worktree creation"
- "Create approved worktree and branch"

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <SuggestWorktreeName/>
**STEP 2:** Execute <GetUserApproval/>
**STEP 3:** Execute <CreateWorktreeAndSwitch/>
</ExecutionSteps>

<SuggestWorktreeName>
Mark first todo as in_progress.

**Validate Git Repository:**
- Use Bash tool to run `git rev-parse --show-toplevel` to confirm we're in a git repository
- If not in git repository, display 'Not in git repository' and STOP

**Process Arguments and Generate Name:**
- If $ARGUMENTS provided and not empty: use $ARGUMENTS as suggested worktree name
- If $ARGUMENTS empty:
  - Use Bash tool to run `git branch --show-current` to get current branch name
  - Generate worktree name suggestion using format: `worktree-[current-branch]-[timestamp]`

**Check for Conflicts:**
- Use Bash tool to run `git worktree list` to check existing worktrees
- If suggested name already exists as worktree, append `-2`, `-3`, etc. until unique name found
- Validate name doesn't contain invalid characters (/, \, :, *, ?, ", <, >, |)
- If invalid characters found, display 'Invalid characters in worktree name' and ask user for different name

**Present Suggestion:**
- Present suggestion to user: "I suggest creating worktree: '[suggested-name]' with branch '[branch-name]'. Do you approve this name or would you like to use a different one?"

Mark first todo as completed when suggestion is presented.
</SuggestWorktreeName>

<GetUserApproval>
Mark second todo as in_progress. Present the suggestion with proper keyword format:

## Available Actions
- **approve** - Accept the suggested worktree name and proceed
- **modify** - Provide a different name for the worktree
- **stop** - Exit without creating worktree

Please select one of the keywords above.

STOP and wait for user response. If user provides alternative name, use their name. If user approves, use suggested name. Mark second todo as completed after receiving user confirmation.
</GetUserApproval>

<CreateWorktreeAndSwitch>
Mark third todo as in_progress.

**Create Worktree:**
- Use Bash tool to run `git worktree add ../[worktree-name] -b [branch-name]` to create worktree and branch
- If command fails, display error message and ask user for guidance

**Switch and Verify:**
- Use Bash tool to run `cd ../[worktree-name]` to switch to new worktree directory
- Confirm success by running `pwd` and `git branch --show-current` to verify location and branch
- Inform user: "Worktree '[worktree-name]' created successfully. You are now working in the new worktree on branch '[branch-name]'."

Mark third todo as completed when finished.
</CreateWorktreeAndSwitch>
