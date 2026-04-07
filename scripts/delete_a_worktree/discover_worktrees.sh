#!/bin/bash
# Discovers available worktrees for deletion
# Usage: discover_worktrees.sh
# Returns: Current context and worktree listing

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "Error: Current directory is not a git repository"
    exit 1
fi

echo "=== Current Context ==="
echo "Directory: $(pwd)"
echo "Branch: $(git branch --show-current)"
echo ""
CURRENT_DIR=$(pwd)
PROTECTED_BRANCH="main"

echo "=== Deletable Worktrees ==="
git worktree list | while IFS= read -r line; do
    # Skip the current worktree
    wt_path=$(echo "$line" | awk '{print $1}')
    [ "$wt_path" = "$CURRENT_DIR" ] && continue
    # Skip protected branch (main)
    echo "$line" | grep -q "\[$PROTECTED_BRANCH\]" && continue
    echo "$line"
done
