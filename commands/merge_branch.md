I'll discover available branches and help you safely merge changes.

The command supports two linear-history operations:

- Update the current branch by rebasing it on top of the selected branch.
- Integrate the selected branch by rebasing it on top of the current branch, then fast-forward merging it.

When neither direction is appropriate, the command can fall back to an explicit `--no-ff` merge commit if the user opts in.

**STEP 1: Discovery**

Run `bash ~/.claude/scripts/merge_branch/discovery.sh`

- If `status` is `"error"`, report the message and STOP
- If `is_clean` is `false`, STOP and ask user to commit or stash first
- Identify clean-fix style-fix candidates from the discovery results. A candidate is a branch
  named `refactor/style` or starting with `refactor/style/` whose `worktree` basename ends in
  `_style_fix`.
- If exactly one clean-fix style-fix candidate exists, automatically select it: set
  `SOURCE_BRANCH` to that candidate's branch name, tell the user which branch/worktree was
  selected, and continue directly to STEP 2. Do not show the selection menu.
- If more than one clean-fix style-fix candidate exists, list those candidates with their
  worktree paths and STOP. Do not choose among multiple style-fix worktrees automatically and do
  not continue the merge flow.
- If no clean-fix style-fix candidate exists, present the branches list to the user clearly,
  numbered for easy selection, showing the last commit for each
- If a branch has a `worktree` field, show the worktree path next to it (e.g. "[worktree: /path/to/wt]")

## Available Actions
- **select** - Choose a branch to merge from by entering its name or number
- **cancel** - Exit without performing merge

[STOP and wait for user response]

If no clean-fix style-fix candidate was auto-selected, store the user's selection as
SOURCE_BRANCH (must match a name from the discovery results). If user input doesn't match,
display error and re-ask.

**STEP 2: Validation**

Run `bash ~/.claude/scripts/merge_branch/validate.sh ${SOURCE_BRANCH}` with `dangerouslyDisableSandbox: true`.

This script performs a real `git merge --no-commit --no-ff` followed by `git merge --abort` to test feasibility. The sandbox blocks git from writing `.git/*.lock` files mid-merge, which leaves the working tree in a half-merged state with untracked files that `git merge --abort` cannot clean up. Always disable the sandbox for this call.

- If `status` is `"error"`, report the message and STOP
- If `current_behind_remote` is `true`, STOP and ask user if they want to pull first
- If `merge_feasible` is `false`, conflicts exist on overlapping hunks — both rebase and merge would hit them. STOP and ask the user to resolve manually.
- If `already_up_to_date` is `true`, the source branch is fully contained in the current branch. Tell the user the merge is a no-op (`Already up to date — ${SOURCE_BRANCH} is an ancestor of ${current_branch}`) and STOP.

Capture from the JSON: `ff_possible`, `source_worktree`, `current_branch`.

**STEP 2.5: Style-flow detection**

If `${SOURCE_BRANCH}` is `refactor/style` or starts with `refactor/style/`, set STYLE_FLOW and announce:

> Style-fix branch detected. On successful merge I will run `/validate_and_push`; on successful
> validate_and_push I will run `/worktree_delete` on `${source_worktree}` (branch `${SOURCE_BRANCH}`).
> A failure at any step stops the chain — later steps will not run.

If `source_worktree` is empty, omit the worktree_delete part of the announcement and skip STEP 7 later.

**STEP 3: Branch by ff-possibility**

If `ff_possible` is `true`, jump to STEP 5 (fast-forward merge).

If `ff_possible` is `false`, the source branch is not a direct descendant of `${current_branch}`. A plain merge would produce an `ort` merge commit, breaking linear history. Offer the user a choice using the actual branch names:

## Source branch is not a fast-forward of `${current_branch}`

Choose what history should look like:

- **rebase-current** - Rebase `${current_branch}` on top of `${SOURCE_BRANCH}`. This takes the commits unique to `${current_branch}` and replays them after `${SOURCE_BRANCH}`. Use this to update a feature branch from `main`. Rewrites SHAs on `${current_branch}`.
- **rebase-source** - Rebase `${SOURCE_BRANCH}` on top of `${current_branch}` (in `${source_worktree}`), then fast-forward merge. This takes the commits unique to `${SOURCE_BRANCH}` and replays them after `${current_branch}`. Use this when integrating another local branch into the current branch. Rewrites SHAs on `${SOURCE_BRANCH}`.
- **merge** - Create an explicit merge commit. Runs `git merge --no-ff` and does not rewrite either branch. Use this when the branch history is already shared and rewriting is not acceptable.
- **cancel** - Exit without merging.

If `source_worktree` is empty, the **rebase-source** option requires the user to check out the branch somewhere first; explain this in the prompt and remove **rebase-source** from the menu. Keep **rebase-current** available.

[STOP and wait for user response]

**STEP 4A: Rebase current branch (only if user chose "rebase-current")**

Run `git rebase ${SOURCE_BRANCH}` with `dangerouslyDisableSandbox: true`.

- If the rebase succeeds, report success: `${current_branch}` was rebased on top of `${SOURCE_BRANCH}`. STOP.
- If the rebase conflicts, report the conflicted files from `git diff --name-only --diff-filter=U` and STOP. The user resolves conflicts in the current worktree, runs `git add <files> && git rebase --continue` (or `git rebase --abort`), and re-invokes `/merge_branch`.
- If the command returns another error, report the error and STOP.

**STEP 4B: Rebase source branch (only if user chose "rebase-source")**

Run `bash ~/.claude/scripts/merge_branch/rebase_source.sh ${SOURCE_BRANCH} ${current_branch} ${source_worktree}` with `dangerouslyDisableSandbox: true`.

- If `status` is `"success"`, continue to STEP 5.
- If `status` is `"conflict"`, the source worktree is paused mid-rebase. Report the message and the `conflicted_files` list to the user, then STOP. The user resolves conflicts in `${source_worktree}` (their editor, normal `<<<<<<<` markers), runs `git add <files> && git rebase --continue` (or `git rebase --abort`), and re-invokes `/merge_branch`.
- If `status` is `"error"`, report the message and STOP.

**STEP 5: Merge**

If the user chose **merge** (non-ff fallback) in STEP 3:

Run `git merge --no-ff ${SOURCE_BRANCH} -m "Merge branch '${SOURCE_BRANCH}'"` with `dangerouslyDisableSandbox: true`.

Otherwise (ff-possible from the start, or after a successful **rebase-source**):

Run `git merge --ff-only ${SOURCE_BRANCH}` with `dangerouslyDisableSandbox: true`. This will fast-forward; no merge commit is created.

Report success to user, including which path was taken (ff-only, rebase-source + ff-only, rebase-current, or explicit `--no-ff` merge).

If the merge command failed, report the error and STOP — do not run STEP 6 or 7.
If STYLE_FLOW is not set, STOP here. Otherwise continue to STEP 6.

**STEP 6: Chained validate_and_push (STYLE_FLOW only)**

Runs only after a successful STEP 5 merge. Invoke the `validate_and_push` skill via the Skill
tool and follow its instructions, including its `needs_pr_branch` handling. If any validation,
push, CI, or merge step fails, report the failing step and STOP — do not delete the worktree.

**STEP 7: Chained worktree_delete (STYLE_FLOW only)**

Runs only after STEP 6 reports its success summary. Invoke the `worktree_delete` skill via the
Skill tool with `${source_worktree}` / `${SOURCE_BRANCH}` as the target. All of that skill's
gates remain in force — protected-branch check, uncommitted-changes check, unpushed-commits
check, and the explicit final confirmation. If any gate fails or the user does not confirm, STOP.
