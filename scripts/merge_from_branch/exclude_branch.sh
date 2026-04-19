#!/bin/bash
# Adds a branch to the merge-from-branch exclusion list for this repo.
# Writes to .git/config via `git config --local`, so it never gets committed.
# Usage: exclude_branch.sh <branch-name>

set -e

BRANCH="$1"

if [[ -z "$BRANCH" ]]; then
    echo "Error: branch name required"
    echo "Usage: exclude_branch.sh <branch-name>"
    exit 1
fi

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "Error: Current directory is not a git repository"
    exit 1
fi

# Warn (but do not fail) if the branch does not currently exist
if ! git rev-parse --verify --quiet "refs/heads/$BRANCH" >/dev/null 2>&1 && \
   ! git rev-parse --verify --quiet "refs/remotes/$BRANCH" >/dev/null 2>&1; then
    echo "Warning: '$BRANCH' does not exist as a local or remote-tracking ref; adding anyway."
fi

# No-op if already excluded
if git config --get-all merge-from-branch.exclude 2>/dev/null | grep -Fxq "$BRANCH"; then
    echo "'$BRANCH' is already excluded from /merge_from_branch."
    exit 0
fi

git config --local --add merge-from-branch.exclude "$BRANCH"
echo "Added '$BRANCH' to merge-from-branch excludes (.git/config, local only)."
