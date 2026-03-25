I'll discover available worktrees and help you safely merge changes from another worktree.

**STEP 1: Discovery**

Run `bash ~/.claude/scripts/merge_from_worktree/discovery.sh`

- If `status` is `"error"`, report the message and STOP
- If `is_clean` is `false`, STOP and ask user to commit or stash first
- Present the worktrees list to the user clearly, numbered for easy selection

## Available Actions
- **select** - Choose a worktree to merge from by entering its path or number
- **cancel** - Exit without performing merge

[STOP and wait for user response]

Store the user's selection as SELECTED_WORKTREE (must match a path from the discovery results).
If user input doesn't match, display error and re-ask.

**STEP 2: Validation**

Run `bash ~/.claude/scripts/merge_from_worktree/validate.sh ${SELECTED_WORKTREE}`

- If `status` is `"error"`, report the message and STOP
- If `source_is_clean` is `false`, STOP and inform user the source worktree has uncommitted changes
- If `current_behind_remote` is `true`, STOP and ask user if they want to pull first
- If `merge_feasible` is `false`, STOP and inform user of merge conflicts

**STEP 3: Merge**

Run `git merge ${SOURCE_BRANCH} -m "Merge branch '${SOURCE_BRANCH}' from worktree ${SELECTED_WORKTREE}"`

where SOURCE_BRANCH comes from the validation result's `source_branch` field.

Report success to user.
