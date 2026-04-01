#!/bin/bash
# Removes a worktree and deletes its branch.
# Also tears down any per-project lint watchers that could recreate the path.
# Usage: perform_deletion.sh <worktree_path> <branch_name>
# Returns: Status of removal and branch deletion

set -euo pipefail

WORKTREE_PATH="$1"
BRANCH_NAME="$2"

if [[ -z "$WORKTREE_PATH" || -z "$BRANCH_NAME" ]]; then
    echo "Error: Usage: perform_deletion.sh <worktree_path> <branch_name>"
    exit 1
fi

stop_project_watchers() {
    local project_dir="$1"
    local found=false

    while IFS= read -r line; do
        local pid
        pid=$(echo "$line" | awk '{print $1}')
        [[ -z "$pid" ]] && continue

        found=true
        echo "Stopping watcher PID $pid for $project_dir"
        pkill -P "$pid" 2>/dev/null || true
        kill "$pid" 2>/dev/null || true
    done < <(
        ps -axo pid=,command= |
            grep -F "$project_dir" |
            grep -E 'cargo-watch|run-lint\.sh' |
            grep -v grep || true
    )

    if [[ "$found" == true ]]; then
        sleep 1

        while IFS= read -r line; do
            local pid
            pid=$(echo "$line" | awk '{print $1}')
            [[ -z "$pid" ]] && continue

            echo "Force stopping watcher PID $pid for $project_dir"
            pkill -P "$pid" 2>/dev/null || true
            kill -9 "$pid" 2>/dev/null || true
        done < <(
            ps -axo pid=,command= |
                grep -F "$project_dir" |
                grep -E 'cargo-watch|run-lint\.sh' |
                grep -v grep || true
        )
    fi
}

cleanup_residual_directory() {
    local project_dir="$1"

    if [[ -e "$project_dir" ]]; then
        echo "Removing residual directory: $project_dir"
        rm -rf "$project_dir"
    fi
}

echo "Stopping project watchers for: $WORKTREE_PATH"
stop_project_watchers "$WORKTREE_PATH"

echo "Removing worktree: $WORKTREE_PATH"
if ! git worktree remove "$WORKTREE_PATH"; then
    echo "Error: Failed to remove worktree"
    exit 1
fi
echo "Worktree removed."

echo ""
echo "Stopping any respawned watchers for: $WORKTREE_PATH"
stop_project_watchers "$WORKTREE_PATH"
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
