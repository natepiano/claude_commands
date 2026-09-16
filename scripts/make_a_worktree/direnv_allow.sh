#!/usr/bin/env bash
# Approves a worktree's .envrc so direnv-gated tooling (cargo-port lints, etc.)
# can build the environment instead of reporting it unavailable.
# Usage: direnv_allow.sh <worktree_path>

set -e

WORKTREE_PATH="$1"

if [ -z "$WORKTREE_PATH" ]; then
    echo "Error: worktree path required"
    exit 1
fi

if [ ! -d "$WORKTREE_PATH" ]; then
    echo "Error: worktree path does not exist: $WORKTREE_PATH"
    exit 1
fi

if [ ! -f "$WORKTREE_PATH/.envrc" ]; then
    echo "No .envrc in worktree - nothing to allow"
    exit 0
fi

if ! command -v direnv >/dev/null 2>&1; then
    echo "Warning: direnv not on PATH - .envrc left blocked"
    exit 0
fi

direnv allow "$WORKTREE_PATH"
echo "Allowed .envrc in $WORKTREE_PATH"

# Prove the environment actually loads - a blocked or broken .envrc is exactly
# what shows up later as a yellow lint icon in cargo-port.
if direnv exec "$WORKTREE_PATH" /bin/sh -c : 2>/dev/null; then
    echo "Environment probe passed"
else
    echo "Warning: direnv exec still fails in $WORKTREE_PATH - lints will report the environment unavailable"
fi

echo "Done"
