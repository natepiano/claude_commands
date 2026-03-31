#!/bin/bash
# Validates a selected source branch and tests merge feasibility
# If the branch has an associated worktree, also checks for uncommitted changes there
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

# Check if this branch has an associated worktree
WORKTREE_PATH=""
SOURCE_IS_CLEAN=true
while IFS= read -r line; do
    WT_PATH=$(echo "$line" | awk '{print $1}')
    WT_BRANCH=$(echo "$line" | grep -o '\[.*\]' | tr -d '[]')
    if [[ "$WT_BRANCH" == "$SOURCE_BRANCH" ]]; then
        WORKTREE_PATH="$WT_PATH"
        break
    fi
done < <(git worktree list)

# If worktree exists, check for uncommitted changes in it
if [[ -n "$WORKTREE_PATH" ]]; then
    if [[ -n "$(git -C "$WORKTREE_PATH" status --porcelain)" ]]; then
        SOURCE_IS_CLEAN=false
    fi
fi

# Check if current branch is behind remote
HAS_REMOTE=false
CURRENT_BEHIND_REMOTE=false
BEHIND_COUNT=0

if git remote get-url origin >/dev/null 2>&1; then
    HAS_REMOTE=true
    git fetch origin >/dev/null 2>&1
    if git rev-parse --abbrev-ref @{upstream} >/dev/null 2>&1; then
        BEHIND_COUNT=$(git rev-list HEAD..@{upstream} --count 2>/dev/null || echo "0")
        if [[ "$BEHIND_COUNT" -gt 0 ]]; then
            CURRENT_BEHIND_REMOTE=true
        fi
    fi
fi

# Test merge feasibility (dry-run)
MERGE_FEASIBLE=true
if ! git merge --no-commit --no-ff "$SOURCE_BRANCH" >/dev/null 2>&1; then
    MERGE_FEASIBLE=false
fi
git merge --abort >/dev/null 2>&1

# Build worktree fields for JSON
WT_FIELDS=""
if [[ -n "$WORKTREE_PATH" ]]; then
    WT_FIELDS="\"worktree\": \"$WORKTREE_PATH\",
  \"source_is_clean\": $SOURCE_IS_CLEAN,"
fi

echo "{
  \"status\": \"success\",
  \"source_branch\": \"$SOURCE_BRANCH\",
  $WT_FIELDS
  \"has_remote\": $HAS_REMOTE,
  \"current_behind_remote\": $CURRENT_BEHIND_REMOTE,
  \"behind_count\": $BEHIND_COUNT,
  \"merge_feasible\": $MERGE_FEASIBLE
}"
