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
IS_CLEAN=true
if [[ -n "$(git status --porcelain)" ]]; then
    IS_CLEAN=false
fi

# Parse worktree list, excluding current
WORKTREE_ENTRIES=""
WORKTREE_COUNT=0
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

    if [[ -n "$WORKTREE_ENTRIES" ]]; then
        WORKTREE_ENTRIES="$WORKTREE_ENTRIES, "
    fi
    WORKTREE_ENTRIES="$WORKTREE_ENTRIES{\"path\": \"$WT_PATH\", \"branch\": \"$WT_BRANCH\"}"
    WORKTREE_COUNT=$((WORKTREE_COUNT + 1))
done < <(git worktree list)

if [[ "$WORKTREE_COUNT" -eq 0 ]]; then
    echo '{"status": "error", "message": "No other worktrees available to merge from"}'
    exit 1
fi

echo "{
  \"status\": \"success\",
  \"current_worktree\": \"$CURRENT_WORKTREE\",
  \"current_branch\": \"$CURRENT_BRANCH\",
  \"is_clean\": $IS_CLEAN,
  \"worktrees\": [$WORKTREE_ENTRIES]
}"
