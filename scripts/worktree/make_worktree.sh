#!/usr/bin/env bash
# Make and record a worktree from its source repository.
set -euo pipefail

if [[ $# != 2 ]]; then
    echo 'usage: make_worktree.sh <path> <branch>' >&2
    exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$script_dir/../lib/py" "$script_dir/worktrees.py" make "$1" "$2"
