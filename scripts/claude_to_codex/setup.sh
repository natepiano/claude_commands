#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.natemccoy.claude-to-codex-sync"
PLIST_NAME="${LABEL}.plist"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
LOG_DIR="/tmp/claude-to-codex-sync"

changes=0

mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

if [[ -L "$PLIST_DST" ]]; then
    current_target="$(readlink "$PLIST_DST")"
    if [[ "$current_target" != "$PLIST_SRC" ]]; then
        rm "$PLIST_DST"
        ln -s "$PLIST_SRC" "$PLIST_DST"
        echo "Updated symlink $PLIST_DST -> $PLIST_SRC"
        changes=$((changes + 1))
    fi
elif [[ -e "$PLIST_DST" ]]; then
    echo "WARNING: $PLIST_DST exists but is not a symlink; leaving it unchanged."
    echo "Remove it manually if you want this script to manage it."
else
    ln -s "$PLIST_SRC" "$PLIST_DST"
    echo "Symlinked $PLIST_DST -> $PLIST_SRC"
    changes=$((changes + 1))
fi

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
    echo "Reloaded launchd agent"
    changes=$((changes + 1))
else
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
    echo "Loaded launchd agent"
    changes=$((changes + 1))
fi

if (( changes == 0 )); then
    echo "Already set up."
else
    echo "Setup complete ($changes change(s))."
fi
