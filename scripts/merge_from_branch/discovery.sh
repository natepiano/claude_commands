#!/bin/bash
# Discovers available local branches and validates current working tree for merge
# Usage: discovery.sh
# Returns: JSON with current state and available branch targets

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

# Check for uncommitted changes
IS_CLEAN=True
if [[ -n "$(git status --porcelain)" ]]; then
    IS_CLEAN=False
fi

# List local branches with last commit, excluding current
BRANCHES_JSON="[]"
while IFS= read -r branch; do
    # Strip leading whitespace and any * prefix
    branch=$(echo "$branch" | sed 's/^[* ]*//')

    # Skip current branch
    if [[ "$branch" == "$CURRENT_BRANCH" ]]; then
        continue
    fi

    LAST_COMMIT=$(git log -1 --pretty=format:'%h %s' "$branch" 2>/dev/null)

    BRANCHES_JSON=$(echo "$BRANCHES_JSON" | python3 -c "
import json, sys
branches = json.load(sys.stdin)
branches.append({'name': $(python3 -c "import json; print(json.dumps('$branch'))"), 'last_commit': $(python3 -c "import json; print(json.dumps('''$LAST_COMMIT'''[0:120]))")})
print(json.dumps(branches))
")
done < <(git branch --format='%(refname:short)')

# Check if any branches available
BRANCH_COUNT=$(echo "$BRANCHES_JSON" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
if [[ "$BRANCH_COUNT" -eq 0 ]]; then
    echo '{"status": "error", "message": "No other local branches available to merge from"}'
    exit 1
fi

python3 -c "
import json
result = {
    'status': 'success',
    'current_branch': '$CURRENT_BRANCH',
    'is_clean': $IS_CLEAN,
    'branches': $BRANCHES_JSON
}
print(json.dumps(result, indent=2))
"
