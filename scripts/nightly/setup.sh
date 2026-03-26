#!/bin/bash
# Setup script for the nightly Rust clean+rebuild launchd agent.
# Idempotent — safe to run multiple times. Only acts on what's missing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.natemccoy.nightly-rust-clean-build.plist"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
LABEL="com.natemccoy.nightly-rust-clean-build"

changes=0

# 1. Create runtime directories
for dir in "$HOME/.local/logs" "$HOME/.local/state/nightly-rust"; do
    if [[ ! -d "$dir" ]]; then
        mkdir -p "$dir"
        echo "Created $dir"
        changes=$((changes + 1))
    fi
done

# 2. Symlink plist into ~/Library/LaunchAgents/
mkdir -p "$HOME/Library/LaunchAgents"
if [[ -L "$PLIST_DST" ]]; then
    current_target=$(readlink "$PLIST_DST")
    if [[ "$current_target" == "$PLIST_SRC" ]]; then
        : # symlink already correct
    else
        rm "$PLIST_DST"
        ln -s "$PLIST_SRC" "$PLIST_DST"
        echo "Updated symlink $PLIST_DST -> $PLIST_SRC (was -> $current_target)"
        changes=$((changes + 1))
    fi
elif [[ -e "$PLIST_DST" ]]; then
    echo "WARNING: $PLIST_DST exists but is not a symlink — skipping."
    echo "  Remove it manually if you want this script to manage it."
else
    ln -s "$PLIST_SRC" "$PLIST_DST"
    echo "Symlinked $PLIST_DST -> $PLIST_SRC"
    changes=$((changes + 1))
fi

# 3. Load the launchd agent if not already loaded
if launchctl list "$LABEL" &>/dev/null; then
    if (( changes > 0 )); then
        # Plist changed — reload
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
        echo "Reloaded launchd agent (plist changed)"
    fi
else
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
    echo "Loaded launchd agent"
    changes=$((changes + 1))
fi

if (( changes == 0 )); then
    echo "Already set up — nothing to do."
else
    echo "Setup complete ($changes change(s))."
fi
