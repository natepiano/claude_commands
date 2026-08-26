#!/bin/bash
# Removes a worktree and deletes its branch.
# Usage: perform_deletion.sh <worktree_path> <branch_name>
# Returns: Status of removal and branch deletion
#
# Requires a confirmation nonce written by the /worktree_delete confirm step
# (record_confirmation.sh). Without it this script refuses to delete, so a
# caller that skips the confirmation step cannot trigger a deletion.

set -euo pipefail

WORKTREE_PATH="${1:-}"
BRANCH_NAME="${2:-}"

if [[ -z "$WORKTREE_PATH" || -z "$BRANCH_NAME" ]]; then
    echo "Error: Usage: perform_deletion.sh <worktree_path> <branch_name>"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=confirm_gate.sh
source "$SCRIPT_DIR/confirm_gate.sh"

if ! wt_check_and_consume "$WORKTREE_PATH" "$BRANCH_NAME"; then
    exit 1
fi

cleanup_residual_directory() {
    local project_dir="$1"

    if [[ -e "$project_dir" ]]; then
        echo "Removing residual directory: $project_dir"
        rm -rf "$project_dir"
    fi
}

# Capture the clean-fix identity BEFORE removing the worktree. A *_style_fix
# worktree's dir name may encode its source checkout (e.g.
# bevy_lagrange_flycam_style_fix), so the history key comes from the
# .fix-project marker, not the basename — and the marker is gone once the
# worktree is removed below. Fall back to the basename strip for legacy
# worktrees created before the marker existed.
WORKTREE_NAME="$(basename "$WORKTREE_PATH")"
STYLE_FIX_PROJECT=""
if [[ "$WORKTREE_NAME" == *_style_fix ]]; then
    if [[ -f "$WORKTREE_PATH/.fix-project" ]]; then
        STYLE_FIX_PROJECT="$(tr -d '[:space:]' < "$WORKTREE_PATH/.fix-project")"
    fi
    [[ -z "$STYLE_FIX_PROJECT" ]] && STYLE_FIX_PROJECT="${WORKTREE_NAME%_style_fix}"
fi

# Every git call below runs against the ambient cwd. When the caller's cwd is
# inside the worktree being removed, that directory stops existing partway
# through and git aborts with "Unable to read current working directory". Move
# to the main worktree first — it is the first entry of `git worktree list`, and
# it is never the deletion target.
MAIN_WORKTREE="$(git worktree list --porcelain | sed -n '1s/^worktree //p')"
if [[ -n "$MAIN_WORKTREE" && -d "$MAIN_WORKTREE" ]]; then
    cd "$MAIN_WORKTREE"
else
    echo "Warning: could not resolve the main worktree; staying in $PWD"
fi

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
# PROJECT (identity/history key) was read from the marker before removal above.
if [[ -n "$STYLE_FIX_PROJECT" ]]; then
    PROJECT="$STYLE_FIX_PROJECT"
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
