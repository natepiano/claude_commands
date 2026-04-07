#!/bin/bash
# Copies settings.local.json to a worktree and adds it to the worktree's git exclude
# Usage: copy_settings_local.sh <worktree_path>
# Run from the source project directory

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

# Copy settings.local.json
mkdir -p "$WORKTREE_PATH/.claude"
if [ -f .claude/settings.local.json ]; then
    cp .claude/settings.local.json "$WORKTREE_PATH/.claude/settings.local.json"
    echo "Copied settings.local.json from source project"
elif [ -f ~/.claude/templates/settings_local.json ]; then
    cp ~/.claude/templates/settings_local.json "$WORKTREE_PATH/.claude/settings.local.json"
    echo "Copied settings.local.json from template"
else
    echo "Warning: no settings.local.json found in source or template"
    exit 0
fi

# Add to worktree-local git exclude
git_dir=$(git -C "$WORKTREE_PATH" rev-parse --git-dir)
mkdir -p "$git_dir/info"
if ! grep -qxF 'settings.local.json' "$git_dir/info/exclude" 2>/dev/null; then
    echo 'settings.local.json' >> "$git_dir/info/exclude"
fi

echo "Done"
