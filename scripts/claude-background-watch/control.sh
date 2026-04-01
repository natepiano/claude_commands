#!/bin/bash

set -euo pipefail

LABEL="com.natemccoy.claude-background-watch"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
WATCH_DIR="$HOME/Library/Logs/claude-background-watch"
KILL_MODE_FILE="$WATCH_DIR/kill-enabled"

mkdir -p "$WATCH_DIR"

case "${1:-status}" in
    start)
        launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true
        launchctl kickstart -k "gui/$(id -u)/$LABEL"
        ;;
    stop)
        launchctl bootout "gui/$(id -u)" "$PLIST"
        ;;
    restart)
        launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST"
        launchctl kickstart -k "gui/$(id -u)/$LABEL"
        ;;
    status)
        launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null || echo "$LABEL is not loaded"
        ;;
    enable-kill)
        touch "$KILL_MODE_FILE"
        echo "kill mode enabled"
        ;;
    disable-kill)
        rm -f "$KILL_MODE_FILE"
        echo "kill mode disabled"
        ;;
    tail)
        tail -f "$WATCH_DIR/watch.log" "$WATCH_DIR/events.log"
        ;;
    *)
        echo "usage: $0 {start|stop|restart|status|enable-kill|disable-kill|tail}"
        exit 1
        ;;
esac
