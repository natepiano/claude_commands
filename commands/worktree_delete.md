---
description: Delete a worktree and its branch.
---

PROTECTED_BRANCHES = main
DEFAULT_REMOTE = origin

Track discovery, validation, change checks, confirmation, and deletion with TodoWrite.
Set each step to `in_progress` before execution and `completed` after execution.

<ExecutionSteps>
    1. Execute <DiscoverWorktrees/>.
    2. Execute <ValidateDeletionTarget/>.
    3. If `cleanup` is true, say: "This tree is cleanup; all work is on the target branch."
       Skip <GetFinalConfirmation/>, execute <PerformDeletion/>, and stop.
    4. Execute <CheckUncommittedChanges/>.
    5. Execute <CheckUnpushedCommits/>.
    6. Execute <GetFinalConfirmation/>.
    7. Execute <PerformDeletion/>.
</ExecutionSteps>

<DiscoverWorktrees>
    1. Run `bash ~/.claude/scripts/worktree_delete/discover_worktrees.sh`.
    2. Exclude the current worktree and `${PROTECTED_BRANCHES}` from the deletion options.
    3. Display the current worktree and the deletion options.

    If one option exists, select it and announce its path without a question.
    If no option exists, inform the user and stop.
    If multiple options exist, ask the user to select a number or enter `cancel`.
    Wait for the selection before validation.
</DiscoverWorktrees>

<ValidateDeletionTarget>
    1. Run `bash ~/.claude/scripts/worktree_delete/worktree_delete_validation.sh "$SELECTED_WORKTREE"`.
    2. Read the JSON status.
    3. If the status is `error`, display the message and stop.
    4. Store the validation fields, including `cleanup` and `cleanup_reason`.
</ValidateDeletionTarget>

<CheckUncommittedChanges>
    If `has_uncommitted` is true, stop and tell the user that deletion would lose the changes.
</CheckUncommittedChanges>

<CheckUnpushedCommits>
    If `has_unpushed` is true and `unpushed_count` exceeds zero, warn that deletion would lose those commits and request confirmation.
    If `has_remote_only` is true, warn that deletion removes `origin/[BRANCH_NAME]` and its `remote_only_count` commits.
    Request confirmation for that loss.
</CheckUnpushedCommits>

<GetFinalConfirmation>
    Display these deletion details:

    1. The worktree path.
    2. The local branch name.
    3. `origin/[BRANCH_NAME]`, if `has_remote_branch` is true.
    4. The unpushed commits and the commits that only `origin/[BRANCH_NAME]` holds.

    Ask the user to enter `confirm` for deletion or `cancel` to stop.
    Wait for the response.
    If the user enters `cancel`, stop.
    If the user enters `confirm`, run:
    `bash ~/.claude/scripts/worktree_delete/record_confirmation.sh "$SELECTED_WORKTREE" "$TARGET_BRANCH"`.
    Continue to <PerformDeletion/>.
</GetFinalConfirmation>

<PerformDeletion>
    1. Use `dangerouslyDisableSandbox: true` when sandbox limits prevent deletion outside the permitted paths.
    2. Run `bash ~/.claude/scripts/worktree_delete/perform_deletion.sh "$SELECTED_WORKTREE" "$TARGET_BRANCH"`.
    3. Report the result.

    The script checks and consumes an existing nonce.
    Without a nonce, it repeats the cleanup check before deletion.
    If it reports "no confirmation nonce", repeat validation and the change checks before <GetFinalConfirmation/>.
    If origin deletion fails, report that the worktree and local branch remain.
</PerformDeletion>
