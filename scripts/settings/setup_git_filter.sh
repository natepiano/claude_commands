#!/usr/bin/env bash

set -euo pipefail

# Manual entry point for the git filter setup. Everything here also runs from
# the SessionStart hook via ensure_git_filters.sh, so this exists for an
# explicit run and for the one check a hook should not repeat every session:
# whether jq, which the settings.json filter shells out to, is present at all.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required for the Claude settings Git filter." >&2
  exit 1
fi

"$SCRIPT_DIR/ensure_git_filters.sh"

echo "Configured the Claude Git filters and watcher for $REPO_ROOT"
