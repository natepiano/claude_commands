#!/bin/bash
# Validates worktree deletion target and performs safety checks
# Usage: delete_a_worktree_validation.sh <worktree_path>
# Returns: JSON with validation status and details

SELECTED_WORKTREE="$1"

if [[ -z "$SELECTED_WORKTREE" ]]; then
    echo '{"status": "error", "message": "No worktree path provided"}'
    exit 1
fi

# Check if selected worktree path exists
if [[ ! -d "$SELECTED_WORKTREE" ]]; then
    echo '{"status": "error", "message": "Selected worktree path does not exist"}'
    exit 1
fi

# Verify it's a git repo
if ! git -C "$SELECTED_WORKTREE" rev-parse --show-toplevel >/dev/null 2>&1; then
    echo '{"status": "error", "message": "Selected path is not a git repository"}'
    exit 1
fi

# Get target branch
TARGET_BRANCH=$(git -C "$SELECTED_WORKTREE" branch --show-current)
if [[ -z "$TARGET_BRANCH" ]]; then
    echo '{"status": "error", "message": "Could not determine target branch"}'
    exit 1
fi

# Verify target branch is NOT main (protected branch)
if [[ "$TARGET_BRANCH" == "main" ]]; then
    echo '{"status": "error", "message": "Cannot delete main branch"}'
    exit 1
fi

# Verify not current worktree
CURRENT_DIR=$(pwd)
SELECTED_ABSOLUTE=$(cd "$SELECTED_WORKTREE" && pwd)
if [[ "$CURRENT_DIR" == "$SELECTED_ABSOLUTE" ]]; then
    echo '{"status": "error", "message": "Cannot delete current worktree"}'
    exit 1
fi

# Check for uncommitted changes
UNCOMMITTED=$(git -C "$SELECTED_WORKTREE" status --porcelain)
HAS_UNCOMMITTED=false
if [[ -n "$UNCOMMITTED" ]]; then
    HAS_UNCOMMITTED=true
fi

# Check for unpushed commits
HAS_UNPUSHED=false
UNPUSHED_COUNT=0
if git -C "$SELECTED_WORKTREE" remote get-url origin >/dev/null 2>&1; then
    git -C "$SELECTED_WORKTREE" fetch origin >/dev/null 2>&1
    if git -C "$SELECTED_WORKTREE" rev-parse --abbrev-ref @{upstream} >/dev/null 2>&1; then
        UNPUSHED_COUNT=$(git -C "$SELECTED_WORKTREE" rev-list @{upstream}..HEAD --count 2>/dev/null || echo "0")
        if [[ "$UNPUSHED_COUNT" -gt 0 ]]; then
            HAS_UNPUSHED=true
        fi
    fi
fi

echo "{
  \"status\": \"success\",
  \"worktree_path\": \"$SELECTED_WORKTREE\",
  \"target_branch\": \"$TARGET_BRANCH\",
  \"has_uncommitted\": $HAS_UNCOMMITTED,
  \"has_unpushed\": $HAS_UNPUSHED,
  \"unpushed_count\": $UNPUSHED_COUNT
}"