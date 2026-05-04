#!/bin/bash
# Wrapper that gates the nightly build on local wall-clock time, independent
# of launchd's StartCalendarInterval (whose UserEventAgent TZ cache is
# unreliable across timezone changes — see the plist for context).
#
# Invoked every STARTINTERVAL_SECONDS by launchd. Reads `date` (which honors
# /etc/localtime correctly). Runs the real nightly script the first time
# each day the local hour is inside the eligible window AND today has not
# already been stamped. All other fires are no-ops.

set -euo pipefail

TARGET_WINDOW_START=4    # earliest local hour eligible to run (inclusive)
TARGET_WINDOW_END=11     # latest local hour eligible to run (inclusive); widened
                         # so a wake-from-sleep fire after 04:00 still triggers
                         # the daily run rather than skipping the day.

STATE_DIR="$HOME/.local/state/nightly-rust"
STAMP_FILE="$STATE_DIR/last-trigger-date"
NIGHTLY_SCRIPT="$HOME/.claude/scripts/nightly/nightly-rust-clean-build.sh"

mkdir -p "$STATE_DIR"

hour=$(date +%H)
hour=${hour#0}
today=$(date +%Y-%m-%d)
last=$(cat "$STAMP_FILE" 2>/dev/null || true)

if [ "$last" = "$today" ]; then
    exit 0
fi

if [ "$hour" -lt "$TARGET_WINDOW_START" ] || [ "$hour" -gt "$TARGET_WINDOW_END" ]; then
    exit 0
fi

echo "$today" > "$STAMP_FILE"
exec "$NIGHTLY_SCRIPT"
