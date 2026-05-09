I'll discover available branches and help you safely merge changes.

The merge always lands as a fast-forward (linear history). When the source branch is not a direct descendant of the current branch, I'll offer to rebase it first — or, if you opt in, fall back to an explicit `--no-ff` merge commit.

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
- If `merge_feasible` is `false`, conflicts exist on overlapping hunks — both rebase and merge would hit them. STOP and ask the user to resolve manually.
- If `already_up_to_date` is `true`, the source branch is fully contained in the current branch. Tell the user the merge is a no-op (`Already up to date — ${SOURCE_BRANCH} is an ancestor of ${current_branch}`) and STOP.

Capture from the JSON: `ff_possible`, `source_worktree`, `current_branch`.

**STEP 3: Branch by ff-possibility**

If `ff_possible` is `true`, jump to STEP 5 (fast-forward merge).

If `ff_possible` is `false`, the source branch is not a direct descendant of `${current_branch}`. A plain merge would produce an `ort` merge commit, breaking linear history. Offer the user a choice:

## Source branch is not a fast-forward of `${current_branch}`

Choose how to integrate `${SOURCE_BRANCH}`:

- **rebase** - Rebase `${SOURCE_BRANCH}` onto `${current_branch}` (in `${source_worktree}`), then fast-forward merge. Preserves linear history. Rewrites SHAs on `${SOURCE_BRANCH}` — fine for local-only branches; requires `--force-with-lease` if the branch was already pushed.
- **merge** - Accept a merge commit. Runs `git merge --no-ff` and produces an explicit merge commit. Use this when `${SOURCE_BRANCH}` has already been pushed and shared.
- **cancel** - Exit without merging.

If `source_worktree` is empty, the **rebase** option requires the user to check out the branch somewhere first; explain this in the prompt and remove **rebase** from the menu.

[STOP and wait for user response]

**STEP 4: Rebase (only if user chose "rebase")**

Run `bash ~/.claude/scripts/merge_from_branch/rebase_source.sh ${SOURCE_BRANCH} ${current_branch} ${source_worktree}` with `dangerouslyDisableSandbox: true`.

- If `status` is `"success"`, continue to STEP 5.
- If `status` is `"conflict"`, the source worktree is paused mid-rebase. Report the message and the `conflicted_files` list to the user, then STOP. The user resolves conflicts in `${source_worktree}` (their editor, normal `<<<<<<<` markers), runs `git add <files> && git rebase --continue` (or `git rebase --abort`), and re-invokes `/merge_from_branch`.
- If `status` is `"error"`, report the message and STOP.

**STEP 5: Merge**

If the user chose **merge** (non-ff fallback) in STEP 3:

Run `git merge --no-ff ${SOURCE_BRANCH} -m "Merge branch '${SOURCE_BRANCH}'"` with `dangerouslyDisableSandbox: true`.

Otherwise (ff-possible from the start, or after a successful rebase):

Run `git merge --ff-only ${SOURCE_BRANCH}` with `dangerouslyDisableSandbox: true`. This will fast-forward; no merge commit is created.

Report success to user, including which path was taken (ff-only, rebase + ff-only, or explicit `--no-ff` merge).
