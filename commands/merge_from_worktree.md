I'll discover available worktrees and help you safely merge changes from another worktree.

<WorktreeMergeTodos>
- [ ] Verify current working directory is in a git worktree
- [ ] Display current worktree location and branch
- [ ] List all available worktrees
- [ ] Ask user which worktree to merge from
- [ ] Check if current worktree is clean (no uncommitted changes)
- [ ] Fetch latest changes from remote
- [ ] Verify current branch is up to date with its remote tracking branch
- [ ] Validate the source worktree path exists and is a valid git worktree
- [ ] Check if source worktree has uncommitted changes
- [ ] Test if merge would have conflicts
- [ ] If all checks pass, perform the merge
- [ ] If any issues found, stop and ask user for guidance
</WorktreeMergeTodos>

Steps to execute:

1. First, verify we're in a git worktree and discover available options:
   - Run `git rev-parse --show-toplevel` to confirm we're in a git repo
   - Run `git branch --show-current` to get current branch name
   - Run `pwd` to show current working directory
   - Run `git worktree list` to show all worktrees
   - Parse the output to identify other worktrees (excluding current one)
   - Display current worktree and available source worktrees clearly
   - **STOP HERE AND ASK: "Which worktree would you like to merge from?" Wait for user response before continuing.**
   - If not in a git repo, STOP and inform user

**IMPORTANT** YOU MUST STOP before doing any further work and wait for user confirmation!!

2. Check working tree status:
   - Run `git status --porcelain` to check for uncommitted changes
   - If there are uncommitted changes, STOP and ask user to commit or stash first

3. Sync with remote (if exists):
   - Check if 'origin' remote exists with `git remote get-url origin`
   - If origin exists, run `git fetch origin` to get latest remote changes
   - Check if current branch has upstream with `git rev-parse --abbrev-ref @{upstream}`
   - If has upstream, check if behind with `git rev-list HEAD..@{upstream} --count`
   - If behind remote, STOP and ask user if they want to pull first
   - If no origin remote, skip remote sync steps and continue

4. Validate source worktree from user selection:
   - Check if selected worktree path exists
   - Run `git -C $SELECTED_WORKTREE rev-parse --show-toplevel` to verify it's a git repo
   - Run `git -C $SELECTED_WORKTREE branch --show-current` to get source branch
   - Run `git -C $SELECTED_WORKTREE status --porcelain` to check for uncommitted changes
   - If source has uncommitted changes, STOP and inform user

5. Test merge feasibility:
   - Run `git merge --no-commit --no-ff $SOURCE_BRANCH` where SOURCE_BRANCH is from step 4
   - If merge would have conflicts, run `git merge --abort` and STOP to ask user
   - If no conflicts, run `git merge --abort` to undo test merge

6. Perform actual merge (only if all checks pass):
   - Run `git merge $SOURCE_BRANCH -m "Merge branch '$SOURCE_BRANCH' from worktree $SELECTED_WORKTREE"`
   - Report success to user

HAPPY PATH: If all validations pass, proceed with merge without stopping
UNHAPPY PATH: Stop and ask user for guidance if:
- Not in a git worktree
- Have uncommitted changes
- Behind remote
- Source worktree invalid or has uncommitted changes
- Merge would have conflicts
