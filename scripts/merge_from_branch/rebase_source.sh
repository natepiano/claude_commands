#!/bin/bash
# Rebases SOURCE_BRANCH onto TARGET_BRANCH so a subsequent fast-forward merge becomes possible.
# The rebase runs in SOURCE_WORKTREE (passed in) — the source branch must be checked out somewhere.
# Usage: rebase_source.sh <source_branch> <target_branch> <source_worktree>
# Returns: JSON with rebase result. On conflict, leaves the source worktree mid-rebase for the user.

SOURCE_BRANCH="$1"
TARGET_BRANCH="$2"
SOURCE_WORKTREE="$3"

if [[ -z "$SOURCE_BRANCH" || -z "$TARGET_BRANCH" || -z "$SOURCE_WORKTREE" ]]; then
    echo '{"status": "error", "message": "Usage: rebase_source.sh <source_branch> <target_branch> <source_worktree>"}'
    exit 1
fi

if [[ ! -d "$SOURCE_WORKTREE" ]]; then
    echo "{\"status\": \"error\", \"message\": \"Source worktree '$SOURCE_WORKTREE' does not exist. Check out '$SOURCE_BRANCH' somewhere first.\"}"
    exit 1
fi

cd "$SOURCE_WORKTREE" || {
    echo "{\"status\": \"error\", \"message\": \"Could not cd to '$SOURCE_WORKTREE'\"}"
    exit 1
}

# Refuse to rebase if the source worktree has uncommitted changes — git rebase would refuse anyway,
# but report it cleanly here.
if [[ -n "$(git status --porcelain)" ]]; then
    echo "{\"status\": \"error\", \"message\": \"Source worktree '$SOURCE_WORKTREE' has uncommitted changes. Commit or stash before rebasing.\"}"
    exit 1
fi

# Verify the source worktree is on the source branch (sanity check — discovery told us this, but
# the user may have switched branches in the worktree between discovery and now).
ACTUAL_BRANCH=$(git branch --show-current)
if [[ "$ACTUAL_BRANCH" != "$SOURCE_BRANCH" ]]; then
    echo "{\"status\": \"error\", \"message\": \"Source worktree is on '$ACTUAL_BRANCH', not '$SOURCE_BRANCH'.\"}"
    exit 1
fi

# Run the rebase. Capture output for diagnostics.
REBASE_OUTPUT=$(git rebase "$TARGET_BRANCH" 2>&1)
REBASE_EXIT=$?

if [[ $REBASE_EXIT -eq 0 ]]; then
    echo "{
  \"status\": \"success\",
  \"source_branch\": \"$SOURCE_BRANCH\",
  \"target_branch\": \"$TARGET_BRANCH\",
  \"source_worktree\": \"$SOURCE_WORKTREE\"
}"
    exit 0
fi

# Rebase failed. Most common cause: conflicts. Surface the conflicted files and leave the worktree mid-rebase.
CONFLICTED_FILES=$(git diff --name-only --diff-filter=U | tr '\n' '|' | sed 's/|$//' | sed 's/|/\\n/g')

echo "{
  \"status\": \"conflict\",
  \"source_branch\": \"$SOURCE_BRANCH\",
  \"target_branch\": \"$TARGET_BRANCH\",
  \"source_worktree\": \"$SOURCE_WORKTREE\",
  \"conflicted_files\": \"$CONFLICTED_FILES\",
  \"message\": \"Rebase paused with conflicts. Resolve in '$SOURCE_WORKTREE', then run 'git add <files> && git rebase --continue' (or 'git rebase --abort' to bail). Re-invoke /merge_from_branch when done.\"
}"
exit 1
