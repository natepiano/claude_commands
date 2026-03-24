#!/bin/bash
# Validates a selected source branch and tests merge feasibility
# Usage: validate.sh <branch_name>
# Returns: JSON with validation results

SOURCE_BRANCH="$1"

if [[ -z "$SOURCE_BRANCH" ]]; then
    echo '{"status": "error", "message": "No branch name provided"}'
    exit 1
fi

# Verify branch exists locally
if ! git rev-parse --verify "$SOURCE_BRANCH" >/dev/null 2>&1; then
    echo "{\"status\": \"error\", \"message\": \"Branch '$SOURCE_BRANCH' does not exist\"}"
    exit 1
fi

# Verify not trying to merge current branch into itself
CURRENT_BRANCH=$(git branch --show-current)
if [[ "$SOURCE_BRANCH" == "$CURRENT_BRANCH" ]]; then
    echo '{"status": "error", "message": "Cannot merge a branch into itself"}'
    exit 1
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
    'source_branch': '$SOURCE_BRANCH',
    'has_remote': $HAS_REMOTE,
    'current_behind_remote': $CURRENT_BEHIND_REMOTE,
    'behind_count': $BEHIND_COUNT,
    'merge_feasible': $MERGE_FEASIBLE
}
print(json.dumps(result, indent=2))
"
