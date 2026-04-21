I'll discover available branches and help you safely merge changes.

**STEP 1: Discovery**

Run `bash ~/.claude/scripts/merge_from_branch/discovery.sh`

- If `status` is `"error"`, report the message and STOP
- If `is_clean` is `false`, STOP and ask user to commit or stash first
- Present the branches list to the user clearly, numbered for easy selection, showing the last commit for each
- If a branch has a `worktree` field, show the worktree path next to it (e.g. "[worktree: /path/to/wt]")

## Available Actions
- **select** - Choose a branch to merge from by entering its name or number
- **cancel** - Exit without performing merge

[STOP and wait for user response]

Store the user's selection as SOURCE_BRANCH (must match a name from the discovery results).
If user input doesn't match, display error and re-ask.

**STEP 2: Validation**

Run `bash ~/.claude/scripts/merge_from_branch/validate.sh ${SOURCE_BRANCH}` with `dangerouslyDisableSandbox: true`.

This script performs a real `git merge --no-commit --no-ff` followed by `git merge --abort` to test feasibility. The sandbox blocks git from writing `.git/*.lock` files mid-merge, which leaves the working tree in a half-merged state with untracked files that `git merge --abort` cannot clean up. Always disable the sandbox for this call.

- If `status` is `"error"`, report the message and STOP
- If `current_behind_remote` is `true`, STOP and ask user if they want to pull first
- If `merge_feasible` is `false`, STOP and inform user of merge conflicts

**STEP 3: Merge**

Run `git merge ${SOURCE_BRANCH} -m "Merge branch '${SOURCE_BRANCH}'"` with `dangerouslyDisableSandbox: true` (merging writes under `.git` and will fail in the sandbox the same way validation does).

Report success to user.
