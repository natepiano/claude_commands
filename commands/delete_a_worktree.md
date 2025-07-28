I'll discover available worktrees and help you safely delete a worktree and its branch.

<WorktreeDeleteTodos>
- [ ] Verify current working directory is in a git worktree
- [ ] Display current worktree location and branch
- [ ] List all available worktrees (excluding main)
- [ ] Ask user which worktree to delete
- [ ] Verify selected worktree is NOT the main worktree
- [ ] Verify selected worktree is NOT the current worktree
- [ ] Check if target worktree has uncommitted changes
- [ ] Check if target branch has unpushed commits
- [ ] Confirm deletion with user
- [ ] Delete the worktree and branch
</WorktreeDeleteTodos>

Steps to execute:

1. First, verify we're in a git worktree and discover available options:
   - Run `git rev-parse --show-toplevel` to confirm we're in a git repo
   - Run `git branch --show-current` to get current branch name
   - Run `pwd` to show current working directory
   - Run `git worktree list` to show all worktrees
   - Parse the output to identify deletable worktrees (excluding current one and main)
   - Display current worktree and available worktrees for deletion
   - Ask user to specify which worktree they want to delete
   - If not in a git repo, STOP and inform user

2. Validate deletion target:
   - Check if selected worktree path exists
   - Run `git -C $SELECTED_WORKTREE rev-parse --show-toplevel` to verify it's a git repo
   - Run `git -C $SELECTED_WORKTREE branch --show-current` to get target branch
   - Verify target branch is NOT 'main' or 'master'
   - Verify selected worktree is NOT the current working directory
   - If targeting main/master or current worktree, STOP and inform user

3. Check for uncommitted changes:
   - Run `git -C $SELECTED_WORKTREE status --porcelain` to check for uncommitted changes
   - If target has uncommitted changes, STOP and inform user they'll be lost

4. Check for unpushed commits (if remote exists):
   - Check if 'origin' remote exists with `git remote get-url origin`
   - If origin exists, run `git fetch origin` to get latest remote changes
   - Run `git -C $SELECTED_WORKTREE rev-parse --abbrev-ref @{upstream}` to check upstream
   - If has upstream, check for unpushed commits with `git -C $SELECTED_WORKTREE rev-list @{upstream}..HEAD --count`
   - If has unpushed commits, WARN user they'll be lost and ask for confirmation
   - If no origin remote, skip remote sync steps and continue

5. Final confirmation:
   - Display summary of what will be deleted:
     - Worktree path
     - Branch name
     - Whether it has unpushed commits
   - Ask user to confirm deletion with explicit "yes" response
   - If user doesn't confirm with "yes", STOP

6. Perform deletion (only if all checks pass and confirmed):
   - Run `git worktree remove $SELECTED_WORKTREE` to remove the worktree
   - Run `git branch -D $TARGET_BRANCH` to delete the branch
   - Report success to user

HAPPY PATH: If all validations pass and user confirms, proceed with deletion
UNHAPPY PATH: Stop and ask user for guidance if:
- Not in a git worktree
- Trying to delete main/master worktree or branch
- Trying to delete current worktree
- Target worktree has uncommitted changes (unless user acknowledges)
- Target branch has unpushed commits (unless user acknowledges)
- User doesn't explicitly confirm deletion