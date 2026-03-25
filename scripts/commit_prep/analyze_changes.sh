#!/bin/bash
# Gathers git status and diff for commit preparation
# Usage: analyze_changes.sh
# Returns: Combined git status and diff output

# Verify current directory is a git repo
if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "Error: Current directory is not a git repository"
    exit 1
fi

# Check for any uncommitted changes
if [[ -z "$(git status --porcelain)" ]]; then
    echo "No uncommitted changes found."
    exit 0
fi

echo "=== Git Status ==="
git status
echo ""
echo "=== Unstaged Changes ==="
git diff
echo ""
echo "=== Staged Changes ==="
git diff --cached
