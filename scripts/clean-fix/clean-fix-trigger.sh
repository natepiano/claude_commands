#!/bin/bash
# Launchd wrapper for clean-fix.sh, invoked every StartInterval seconds by
# com.natemccoy.style-fix. There is no idle gate, so every firing runs the
# style pipeline and keeps the eval/review/fix queue full.
#
# Concurrency guard: pgrep against the orchestrator's path.
# clean-fix.sh runs synchronously start-to-finish (style-fix-worktrees waits
# on its backgrounded agents before returning), so its presence in the
# process table accurately reflects "a run is still in progress."

set -euo pipefail

CLEAN_FIX_SCRIPT="$HOME/.claude/scripts/clean-fix/clean-fix.sh"

if pgrep -f "$CLEAN_FIX_SCRIPT" >/dev/null 2>&1; then
    exit 0
fi

exec "$CLEAN_FIX_SCRIPT"
