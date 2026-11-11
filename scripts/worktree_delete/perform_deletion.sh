#!/usr/bin/env bash
# Removes a worktree and deletes its branch, locally and on origin.
# Usage: perform_deletion.sh <worktree_path> <branch_name>
# Returns: Status of removal and branch deletion
#
# Cleanup deletion requires all work to satisfy the landing checks.
# Other deletion requires the confirmation nonce from record_confirmation.sh.

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

HAS_CONFIRMATION=false
CHECKED_LOCAL_TIP=""
CHECKED_ORIGIN_TIP=""
if [[ -f "$(wt_confirm_file "$WORKTREE_PATH")" ]]; then
    if ! wt_check_and_consume "$WORKTREE_PATH" "$BRANCH_NAME"; then
        exit 1
    fi
    HAS_CONFIRMATION=true
else
    if ! CHECKED_TIPS=$("$SCRIPT_DIR/../lib/py" "$SCRIPT_DIR/../worktree/worktrees.py" cleanup "$WORKTREE_PATH" --checked-tips 2>&1); then
        wt_check_and_consume "$WORKTREE_PATH" "$BRANCH_NAME" || true
        printf '%s\n' "$CHECKED_TIPS"
        exit 1
    fi
    read -r CHECKED_LOCAL_TIP CHECKED_ORIGIN_TIP <<< "$CHECKED_TIPS"
    if [[ "$(git -C "$WORKTREE_PATH" branch --show-current)" != "$BRANCH_NAME" ]]; then
        wt_check_and_consume "$WORKTREE_PATH" "$BRANCH_NAME" || true
        echo "Error: the worktree branch does not match $BRANCH_NAME."
        exit 1
    fi
    if [[ "$(git -C "$WORKTREE_PATH" rev-parse --path-format=absolute --git-common-dir)" != \
          "$(git rev-parse --path-format=absolute --git-common-dir)" ]]; then
        wt_check_and_consume "$WORKTREE_PATH" "$BRANCH_NAME" || true
        echo "Error: the worktree belongs to a different repository."
        exit 1
    fi
fi

# Refuse a branch lookup fault before origin deletion.
if git show-ref --verify --quiet "refs/heads/$BRANCH_NAME"; then
    if [[ "$HAS_CONFIRMATION" == true ]]; then
        CHECKED_LOCAL_TIP=$(git rev-parse --verify "refs/heads/$BRANCH_NAME")
    fi
else
    BRANCH_LOOKUP_STATUS=$?
    if [[ "$BRANCH_LOOKUP_STATUS" -ne 1 ]]; then
        exit "$BRANCH_LOOKUP_STATUS"
    fi
fi

ORIGIN_DEFAULT="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
CURRENT_BRANCH="$(git branch --show-current)"
if [[ "$BRANCH_NAME" == "main" || "$BRANCH_NAME" == "${ORIGIN_DEFAULT#origin/}" || "$BRANCH_NAME" == "$CURRENT_BRANCH" ]]; then
    echo "Error: refusing to delete protected or current branch: $BRANCH_NAME"
    exit 1
fi

cleanup_residual_directory() {
    local project_dir="$1"

    if [[ -e "$project_dir" ]]; then
        echo "Removing residual directory: $project_dir"
        rm -rf "$project_dir"
    fi
}

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

if [[ "$HAS_CONFIRMATION" == true ]]; then
    REMOTE_NAMES=$(git remote)
    if [[ " ${REMOTE_NAMES//$'\n'/ } " == *" origin "* ]]; then
        if ! ORIGIN_BRANCH=$(git ls-remote --heads origin "refs/heads/$BRANCH_NAME"); then
            echo "Error: cannot read the origin branch; deletion stops."
            exit 1
        fi
        CHECKED_ORIGIN_TIP="${ORIGIN_BRANCH%%[[:space:]]*}"
    fi
fi

if [[ -n "$CHECKED_ORIGIN_TIP" ]]; then
    echo "Deleting origin/$BRANCH_NAME"
    if ! git push "--force-with-lease=refs/heads/$BRANCH_NAME:$CHECKED_ORIGIN_TIP" origin --delete "$BRANCH_NAME"; then
        echo "Error: origin branch deletion failed; the worktree and local branch stay."
        exit 1
    fi
    echo "Remote branch deleted."
fi

"$SCRIPT_DIR/../lib/py" "$SCRIPT_DIR/../lib/scratch.py" cargo-save "$WORKTREE_PATH"

echo "Removing worktree: $WORKTREE_PATH"
if [[ -n "$CHECKED_LOCAL_TIP" ]] && [[ "$(git rev-parse --verify "refs/heads/$BRANCH_NAME")" != "$CHECKED_LOCAL_TIP" ]]; then
    echo "Error: the local branch changed after the cleanup check; the worktree stays."
    exit 1
fi

if ! git worktree remove "$WORKTREE_PATH" 2>/dev/null; then
    if [[ "$HAS_CONFIRMATION" == false ]]; then
        echo "Error: the tree changed after the cleanup check; deletion stops."
        exit 1
    fi
    echo "Standard removal failed, forcing..."
    if ! git worktree remove --force "$WORKTREE_PATH" 2>/dev/null; then
        echo "Force removal failed, pruning and cleaning up manually..."
        git worktree prune
    fi
fi

if [[ "$HAS_CONFIRMATION" == true ]]; then
    cleanup_residual_directory "$WORKTREE_PATH"
fi

if [[ -e "$WORKTREE_PATH" ]]; then
    echo "Error: Worktree path still exists after cleanup: $WORKTREE_PATH"
    exit 1
fi

"$SCRIPT_DIR/../lib/py" "$SCRIPT_DIR/../lib/scratch.py" cargo-clean "$WORKTREE_PATH"

echo ""
echo "Deleting branch: $BRANCH_NAME"
if [[ -n "$CHECKED_LOCAL_TIP" ]]; then
    if ! git update-ref -d "refs/heads/$BRANCH_NAME" "$CHECKED_LOCAL_TIP"; then
        echo "Error: the local branch changed after the cleanup check; the local branch stays."
        exit 1
    fi
fi
if CONFIG_ERROR=$(LC_ALL=C git config --local --remove-section "branch.$BRANCH_NAME" 2>&1); then
    :
else
    CONFIG_STATUS=$?
    if [[ "$CONFIG_STATUS" -ne 128 || "$CONFIG_ERROR" != "fatal: no such section: branch.$BRANCH_NAME" ]]; then
        printf '%s\n' "$CONFIG_ERROR" >&2
        exit "$CONFIG_STATUS"
    fi
fi
echo "Branch deleted."
exit 0
