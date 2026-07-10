#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.natemccoy.codex-agent-catalog-sync"
PLIST_NAME="$LABEL.plist"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
LOG_DIR="/tmp/codex-agent-catalog-sync"
STATE_DIR="$HOME/.local/state/codex-agent-catalog-sync"

mkdir -p "$LOG_DIR" "$STATE_DIR" "$HOME/Library/LaunchAgents"

if [[ -L "$PLIST_DST" ]]; then
    current_target="$(readlink "$PLIST_DST")"
    if [[ "$current_target" != "$PLIST_SRC" ]]; then
        rm "$PLIST_DST"
        ln -s "$PLIST_SRC" "$PLIST_DST"
        echo "Updated symlink $PLIST_DST -> $PLIST_SRC"
    fi
elif [[ -e "$PLIST_DST" ]]; then
    echo "ERROR: $PLIST_DST exists and is not a symlink" >&2
    exit 1
else
    ln -s "$PLIST_SRC" "$PLIST_DST"
    echo "Symlinked $PLIST_DST -> $PLIST_SRC"
fi

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
fi
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Loaded launchd agent $LABEL"
