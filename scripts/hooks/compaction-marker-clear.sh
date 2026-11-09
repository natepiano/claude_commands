#!/usr/bin/env bash
# Start Python only when a compaction marker exists.

shopt -s nullglob
markers=("$HOME"/.local/state/compaction/*.json)
(( ${#markers[@]} )) || exit 0

hook_dir="${BASH_SOURCE[0]%/*}"
"$hook_dir/../lib/py" "$hook_dir/compaction-marker-clear.py" 2>/dev/null || true
exit 0
