#!/bin/bash
# Removes a worktree and deletes its branch.
# Usage: perform_deletion.sh <worktree_path> <branch_name>
# Returns: Status of removal and branch deletion

set -euo pipefail

WORKTREE_PATH="$1"
BRANCH_NAME="$2"

if [[ -z "$WORKTREE_PATH" || -z "$BRANCH_NAME" ]]; then
    echo "Error: Usage: perform_deletion.sh <worktree_path> <branch_name>"
    exit 1
fi

cleanup_residual_directory() {
    local project_dir="$1"

    if [[ -e "$project_dir" ]]; then
        echo "Removing residual directory: $project_dir"
        rm -rf "$project_dir"
    fi
}

echo "Removing worktree: $WORKTREE_PATH"
if ! git worktree remove "$WORKTREE_PATH" 2>/dev/null; then
    echo "Standard removal failed, forcing..."
    if ! git worktree remove --force "$WORKTREE_PATH" 2>/dev/null; then
        echo "Force removal failed, pruning and cleaning up manually..."
        git worktree prune
    fi
fi

cleanup_residual_directory "$WORKTREE_PATH"

if [[ -e "$WORKTREE_PATH" ]]; then
    echo "Error: Worktree path still exists after cleanup: $WORKTREE_PATH"
    exit 1
fi

echo ""
echo "Deleting branch: $BRANCH_NAME"
if ! git branch -D "$BRANCH_NAME"; then
    echo "Error: Failed to delete branch"
    exit 1
fi
echo "Branch deleted."

# A <project>_style_fix worktree carries a clean-fix pending JSON in
# fixed_findings state. The history row is already recorded by finalize-fix;
# the pending file is the only leftover, and while it exists every clean-fix
# run skips the project. Remove it here so the cycle can restart.
WORKTREE_NAME="$(basename "$WORKTREE_PATH")"
if [[ "$WORKTREE_NAME" == *_style_fix ]]; then
    PROJECT="${WORKTREE_NAME%_style_fix}"
    HISTORY_HELPER="$HOME/.claude/scripts/clean-fix/style_history.py"
    if [[ -f "$HISTORY_HELPER" ]]; then
        echo ""
        echo "Style-fix worktree deleted — discarding clean-fix pending state for: $PROJECT"
        if python3 "$HISTORY_HELPER" discard-pending --project "$PROJECT"; then
            echo "Pending state discarded."
        else
            echo "Warning: discard-pending failed for $PROJECT — clean-fix will keep skipping it until ~/rust/nate_style/.history/.pending/$PROJECT.json is removed."
        fi
    fi
fi
