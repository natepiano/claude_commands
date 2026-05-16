#!/bin/bash
# Wrapper that gates the nightly build on user inactivity. Invoked every
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

NIGHTLY_SCRIPT="$HOME/.claude/scripts/nightly/nightly-rust-clean-build.sh"

if pgrep -f "$NIGHTLY_SCRIPT" >/dev/null 2>&1; then
    exit 0
fi

idle_seconds=$(ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}')

if [ -z "$idle_seconds" ] || [ "$idle_seconds" -lt "$IDLE_THRESHOLD_SECONDS" ]; then
    exit 0
fi

exec "$NIGHTLY_SCRIPT"
