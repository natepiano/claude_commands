#!/usr/bin/env bash
# Make an approved original work checkout; run from its source repository.
set -euo pipefail

if [[ $# != 2 ]]; then
    echo 'usage: make_worktree.sh <path> <branch>' >&2
    exit 2
fi

exec git worktree add "$1" -b "$2"
