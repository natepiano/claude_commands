#!/bin/bash
# Gathers git status, diff, and repo URL for changelog preparation
# Usage: analyze_changes.sh
# Returns: Combined git status, diff, and remote URL

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "Error: Current directory is not a git repository"
    exit 1
fi

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
echo ""
echo "=== Remote URL ==="
git remote get-url origin 2>/dev/null || echo "(no remote configured)"
