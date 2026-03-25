#!/bin/bash
# Excludes a file from git tracking via .git/info/exclude
# Usage: exclude.sh <filename>
# Returns: Status of each operation performed

FILENAME="$1"

if [[ -z "$FILENAME" ]]; then
    echo "Error: No filename provided"
    exit 1
fi

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "Error: Current directory is not a git repository"
    exit 1
fi

GIT_DIR=$(git rev-parse --git-dir)
EXCLUDE_FILE="$GIT_DIR/info/exclude"

# Ensure .git/info/exclude exists
mkdir -p "$GIT_DIR/info"
touch "$EXCLUDE_FILE"

# Check if already excluded
if grep -qxF "$FILENAME" "$EXCLUDE_FILE"; then
    echo "ALREADY_EXCLUDED: $FILENAME is already listed in .git/info/exclude"
    exit 0
fi

# Check if tracked and remove from tracking if so
WAS_TRACKED=false
if git ls-files --error-unmatch "$FILENAME" >/dev/null 2>&1; then
    WAS_TRACKED=true
    git rm --cached "$FILENAME" >/dev/null 2>&1
    echo "UNTRACKED: Removed $FILENAME from git tracking (local file kept)"
fi

# Append to exclude
echo "$FILENAME" >> "$EXCLUDE_FILE"
echo "EXCLUDED: Added $FILENAME to .git/info/exclude"

if [[ "$WAS_TRACKED" == "false" ]]; then
    echo "NOTE: File was not tracked by git, only added to exclude list"
fi
