#!/bin/bash
# Discovers available local branches and validates current working tree for merge
# Also detects worktrees associated with each branch
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
IS_CLEAN=true
if [[ -n "$(git status --porcelain)" ]]; then
    IS_CLEAN=false
fi

# Capture worktree list once (paths and branches)
WORKTREE_PATHS=()
WORKTREE_BRANCHES=()
while IFS= read -r line; do
    WT_PATH=$(echo "$line" | awk '{print $1}')
    WT_BRANCH=$(echo "$line" | grep -o '\[.*\]' | tr -d '[]')
    if [[ -n "$WT_BRANCH" && "$WT_BRANCH" != "detached HEAD" ]]; then
        WORKTREE_PATHS+=("$WT_PATH")
        WORKTREE_BRANCHES+=("$WT_BRANCH")
    fi
done < <(git worktree list)

# List local branches with last commit, excluding current
BRANCH_ENTRIES=""
BRANCH_COUNT=0
while IFS= read -r branch; do
    # Skip current branch
    if [[ "$branch" == "$CURRENT_BRANCH" ]]; then
        continue
    fi

    LAST_COMMIT=$(git log -1 --pretty=format:'%h %s' "$branch" 2>/dev/null)
    # Escape double quotes in commit message
    LAST_COMMIT=$(echo "$LAST_COMMIT" | sed 's/"/\\"/g')

    # Check if this branch has an associated worktree
    WT_FIELD=""
    for i in "${!WORKTREE_BRANCHES[@]}"; do
        if [[ "${WORKTREE_BRANCHES[$i]}" == "$branch" ]]; then
            WT_FIELD=", \"worktree\": \"${WORKTREE_PATHS[$i]}\""
            break
        fi
    done

    if [[ -n "$BRANCH_ENTRIES" ]]; then
        BRANCH_ENTRIES="$BRANCH_ENTRIES, "
    fi
    BRANCH_ENTRIES="$BRANCH_ENTRIES{\"name\": \"$branch\", \"last_commit\": \"$LAST_COMMIT\"$WT_FIELD}"
    BRANCH_COUNT=$((BRANCH_COUNT + 1))
done < <(git branch --format='%(refname:short)')

if [[ "$BRANCH_COUNT" -eq 0 ]]; then
    echo '{"status": "error", "message": "No other local branches available to merge from"}'
    exit 1
fi

echo "{
  \"status\": \"success\",
  \"current_branch\": \"$CURRENT_BRANCH\",
  \"is_clean\": $IS_CLEAN,
  \"branches\": [$BRANCH_ENTRIES]
}"
