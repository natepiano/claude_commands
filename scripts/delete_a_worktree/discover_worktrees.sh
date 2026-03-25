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
echo "=== All Worktrees ==="
git worktree list
