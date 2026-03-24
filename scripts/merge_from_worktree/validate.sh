#!/bin/bash
# Validates a selected source worktree and tests merge feasibility
# Usage: validate.sh <worktree_path>
# Returns: JSON with validation results

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

# Get source branch
SOURCE_BRANCH=$(git -C "$SELECTED_WORKTREE" branch --show-current)
if [[ -z "$SOURCE_BRANCH" ]]; then
    echo '{"status": "error", "message": "Could not determine source branch (detached HEAD?)"}'
    exit 1
fi

# Check source for uncommitted changes
SOURCE_IS_CLEAN=True
if [[ -n "$(git -C "$SELECTED_WORKTREE" status --porcelain)" ]]; then
    SOURCE_IS_CLEAN=False
fi

# Check if current branch is behind remote
HAS_REMOTE=False
CURRENT_BEHIND_REMOTE=False
BEHIND_COUNT=0

if git remote get-url origin >/dev/null 2>&1; then
    HAS_REMOTE=True
    git fetch origin >/dev/null 2>&1
    if git rev-parse --abbrev-ref @{upstream} >/dev/null 2>&1; then
        BEHIND_COUNT=$(git rev-list HEAD..@{upstream} --count 2>/dev/null || echo "0")
        if [[ "$BEHIND_COUNT" -gt 0 ]]; then
            CURRENT_BEHIND_REMOTE=True
        fi
    fi
fi

# Test merge feasibility (dry-run)
MERGE_FEASIBLE=True
if ! git merge --no-commit --no-ff "$SOURCE_BRANCH" >/dev/null 2>&1; then
    MERGE_FEASIBLE=False
fi
git merge --abort >/dev/null 2>&1

python3 -c "
import json
result = {
    'status': 'success',
    'source_path': '$SELECTED_WORKTREE',
    'source_branch': '$SOURCE_BRANCH',
    'source_is_clean': $SOURCE_IS_CLEAN,
    'has_remote': $HAS_REMOTE,
    'current_behind_remote': $CURRENT_BEHIND_REMOTE,
    'behind_count': $BEHIND_COUNT,
    'merge_feasible': $MERGE_FEASIBLE
}
print(json.dumps(result, indent=2))
"
