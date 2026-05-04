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

# Detect "already up to date": SOURCE_BRANCH is an ancestor of HEAD, so the merge is a no-op.
ALREADY_UP_TO_DATE=false
if git merge-base --is-ancestor "$SOURCE_BRANCH" HEAD 2>/dev/null; then
    ALREADY_UP_TO_DATE=true
fi

# Determine whether a fast-forward merge is possible.
# ff is possible iff HEAD is an ancestor of SOURCE_BRANCH (source already contains every commit on HEAD).
FF_POSSIBLE=false
if git merge-base --is-ancestor HEAD "$SOURCE_BRANCH" 2>/dev/null; then
    FF_POSSIBLE=true
fi

# Locate source branch's worktree, if any (rebase needs to happen wherever the branch is checked out).
SOURCE_WORKTREE=""
while IFS= read -r line; do
    WT_PATH=$(echo "$line" | awk '{print $1}')
    WT_BRANCH=$(echo "$line" | grep -o '\[.*\]' | tr -d '[]')
    if [[ "$WT_BRANCH" == "$SOURCE_BRANCH" ]]; then
        SOURCE_WORKTREE="$WT_PATH"
        break
    fi
done < <(git worktree list)

# Test merge feasibility (dry-run). Predicts conflicts on either an --no-ff merge or a rebase.
MERGE_FEASIBLE=true
MERGE_CHECK_ERROR=""
if ! MERGE_OUTPUT=$(git merge --no-commit --no-ff "$SOURCE_BRANCH" 2>&1); then
    MERGE_FEASIBLE=false
    if echo "$MERGE_OUTPUT" | grep -qiE 'Operation not permitted|cannot lock ref|Unable to create .+\.lock|unable to create temporary file'; then
        MERGE_CHECK_ERROR="Merge feasibility check was blocked by the environment. Git could not write temporary merge state under .git; rerun this validation outside the sandbox."
    fi
fi
git merge --abort >/dev/null 2>&1 || true

if [[ -n "$MERGE_CHECK_ERROR" ]]; then
    echo "{\"status\": \"error\", \"message\": \"$MERGE_CHECK_ERROR\"}"
    exit 1
fi

echo "{
  \"status\": \"success\",
  \"source_branch\": \"$SOURCE_BRANCH\",
  \"current_branch\": \"$CURRENT_BRANCH\",
  \"has_remote\": $HAS_REMOTE,
  \"current_behind_remote\": $CURRENT_BEHIND_REMOTE,
  \"behind_count\": $BEHIND_COUNT,
  \"ff_possible\": $FF_POSSIBLE,
  \"already_up_to_date\": $ALREADY_UP_TO_DATE,
  \"source_worktree\": \"$SOURCE_WORKTREE\",
  \"merge_feasible\": $MERGE_FEASIBLE
}"
