#!/bin/bash
# Wrapper that gates the clean-fix build on user inactivity. Invoked every
# StartInterval seconds by launchd. Starts a new run whenever HID idle time
# exceeds the threshold AND no previous run is still in flight.
#
# HID idle = nanoseconds since the last keyboard/mouse/trackpad event,
# reported by IOKit. System-wide, no per-app awareness (e.g. watching video
# without input still counts as idle).
#
# Concurrency guard: pgrep against the build script's path. The build script
# runs synchronously start-to-finish (style-fix-worktrees waits on its
# backgrounded agents before returning), so its presence in the process
# table accurately reflects "a run is still in progress."

set -euo pipefail

IDLE_THRESHOLD_SECONDS=3600   # 1 hour away from keyboard

CLEAN_FIX_SCRIPT="$HOME/.claude/scripts/clean-fix/clean-fix.sh"

if pgrep -f "$CLEAN_FIX_SCRIPT" >/dev/null 2>&1; then
    exit 0
fi

# `awk … exit` closes the pipe early, so ioreg gets SIGPIPE and the pipeline
# returns 141 under pipefail. `|| true` swallows that without disabling pipefail
# for the rest of the script.
idle_seconds=$(ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}' || true)

if [ -z "$idle_seconds" ] || [ "$idle_seconds" -lt "$IDLE_THRESHOLD_SECONDS" ]; then
    exit 0
fi

exec "$CLEAN_FIX_SCRIPT"
