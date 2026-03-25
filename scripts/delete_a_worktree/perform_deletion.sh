#!/bin/bash
# Removes a worktree and deletes its branch
# Usage: perform_deletion.sh <worktree_path> <branch_name>
# Returns: Status of removal and branch deletion

WORKTREE_PATH="$1"
BRANCH_NAME="$2"

if [[ -z "$WORKTREE_PATH" || -z "$BRANCH_NAME" ]]; then
    echo "Error: Usage: perform_deletion.sh <worktree_path> <branch_name>"
    exit 1
fi

echo "Removing worktree: $WORKTREE_PATH"
if ! git worktree remove "$WORKTREE_PATH"; then
    echo "Error: Failed to remove worktree"
    exit 1
fi
echo "Worktree removed."

echo ""
echo "Deleting branch: $BRANCH_NAME"
if ! git branch -D "$BRANCH_NAME"; then
    echo "Error: Failed to delete branch"
    exit 1
fi
echo "Branch deleted."
