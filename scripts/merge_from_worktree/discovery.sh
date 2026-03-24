#!/bin/bash
# Discovers available worktrees and validates current working tree for merge
# Usage: discovery.sh
# Returns: JSON with current state and available worktree targets

# Verify current directory is a git repo
if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    echo '{"status": "error", "message": "Current directory is not a git repository"}'
    exit 1
fi

# Get current branch
CURRENT_BRANCH=$(git branch --show-current)
if [[ -z "$CURRENT_BRANCH" ]]; then
    echo '{"status": "error", "message": "Could not determine current branch (detached HEAD?)"}'
    exit 1
fi

# Get current worktree path
CURRENT_WORKTREE=$(git rev-parse --show-toplevel)

# Check for uncommitted changes
IS_CLEAN=True
if [[ -n "$(git status --porcelain)" ]]; then
    IS_CLEAN=False
fi

# Parse worktree list, excluding current
WORKTREES_JSON="[]"
while IFS= read -r line; do
    WT_PATH=$(echo "$line" | awk '{print $1}')
    WT_BRANCH=$(echo "$line" | grep -o '\[.*\]' | tr -d '[]')

    # Skip current worktree
    if [[ "$WT_PATH" == "$CURRENT_WORKTREE" ]]; then
        continue
    fi

    # Skip bare repos or detached heads
    if [[ -z "$WT_BRANCH" || "$WT_BRANCH" == "detached HEAD" ]]; then
        continue
    fi

    WORKTREES_JSON=$(echo "$WORKTREES_JSON" | python3 -c "
import json, sys
wts = json.load(sys.stdin)
wts.append({'path': '$WT_PATH', 'branch': '$WT_BRANCH'})
print(json.dumps(wts))
")
done < <(git worktree list)

# Check if any worktrees available
WT_COUNT=$(echo "$WORKTREES_JSON" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
if [[ "$WT_COUNT" -eq 0 ]]; then
    echo '{"status": "error", "message": "No other worktrees available to merge from"}'
    exit 1
fi

python3 -c "
import json
result = {
    'status': 'success',
    'current_worktree': '$CURRENT_WORKTREE',
    'current_branch': '$CURRENT_BRANCH',
    'is_clean': $IS_CLEAN,
    'worktrees': $WORKTREES_JSON
}
print(json.dumps(result, indent=2))
"
