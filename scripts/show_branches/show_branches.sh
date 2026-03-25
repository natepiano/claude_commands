#!/bin/bash
# Shows remote and local git branches
# Usage: show_branches.sh
# Returns: Combined remote and local branch listing

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "Error: Current directory is not a git repository"
    exit 1
fi

echo "=== Remote Branches (origin) ==="
git ls-remote --heads origin 2>/dev/null || echo "(no remote configured)"
echo ""
echo "=== Local Branches ==="
git branch
