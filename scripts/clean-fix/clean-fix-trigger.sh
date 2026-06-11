#!/bin/bash
# Launchd wrapper for clean-fix.sh. Usage: clean-fix-trigger.sh [clean|style]
#
#   clean — invoked nightly (StartCalendarInterval 04:00). Gated on HID idle
#           >= 1 hour so a 4 AM work session skips that night's clean/rebuild.
#           HID idle = nanoseconds since the last keyboard/mouse/trackpad
#           event, reported by IOKit.
#   style — invoked every StartInterval seconds. No idle gate: every firing
#           runs the style pipeline so the eval/review/fix queue stays full.
#
# Concurrency guard (both scopes): pgrep against the orchestrator's path.
# clean-fix.sh runs synchronously start-to-finish (style-fix-worktrees waits
# on its backgrounded agents before returning), so its presence in the
# process table accurately reflects "a run is still in progress" — and since
# both scopes exec the same script path, the guard also prevents the clean
# and style jobs from overlapping each other.

set -euo pipefail

SCOPE="${1:-style}"
case "$SCOPE" in
    clean|style) ;;
    *) echo "Usage: clean-fix-trigger.sh [clean|style]"; exit 1 ;;
esac

IDLE_THRESHOLD_SECONDS=3600   # 1 hour away from keyboard (clean scope only)

CLEAN_FIX_SCRIPT="$HOME/.claude/scripts/clean-fix/clean-fix.sh"

if pgrep -f "$CLEAN_FIX_SCRIPT" >/dev/null 2>&1; then
    exit 0
fi

if [[ "$SCOPE" == "clean" ]]; then
    # `awk … exit` closes the pipe early, so ioreg gets SIGPIPE and the pipeline
    # returns 141 under pipefail. `|| true` swallows that without disabling
    # pipefail for the rest of the script.
    idle_seconds=$(ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}' || true)

    if [ -z "$idle_seconds" ] || [ "$idle_seconds" -lt "$IDLE_THRESHOLD_SECONDS" ]; then
        exit 0
    fi
fi

exec "$CLEAN_FIX_SCRIPT" "$SCOPE"
