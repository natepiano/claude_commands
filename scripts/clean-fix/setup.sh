#!/bin/bash
# Setup script for the clean-fix launchd agents:
#   com.natemccoy.style-fix  — style eval/review/fix every 10 min
#   com.natemccoy.cargo-clean — nightly cargo clean/build/mend + warmup, 4 AM
# Idempotent — safe to run multiple times. Only acts on what's missing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAMES=(
    "com.natemccoy.style-fix.plist"
    "com.natemccoy.cargo-clean.plist"
)

changes=0

# 1. Create runtime directories
for dir in "$HOME/.local/logs" "$HOME/.local/state/clean-fix"; do
    if [[ ! -d "$dir" ]]; then
        mkdir -p "$dir"
        echo "Created $dir"
        changes=$((changes + 1))
    fi
done

mkdir -p "$HOME/Library/LaunchAgents"

for plist_name in "${PLIST_NAMES[@]}"; do
    plist_src="$SCRIPT_DIR/$plist_name"
    plist_dst="$HOME/Library/LaunchAgents/$plist_name"
    label="${plist_name%.plist}"
    plist_changes=0

    # 2. Symlink plist into ~/Library/LaunchAgents/
    if [[ -L "$plist_dst" ]]; then
        current_target=$(readlink "$plist_dst")
        if [[ "$current_target" == "$plist_src" ]]; then
            : # symlink already correct
        else
            rm "$plist_dst"
            ln -s "$plist_src" "$plist_dst"
            echo "Updated symlink $plist_dst -> $plist_src (was -> $current_target)"
            plist_changes=$((plist_changes + 1))
        fi
    elif [[ -e "$plist_dst" ]]; then
        echo "WARNING: $plist_dst exists but is not a symlink — skipping."
        echo "  Remove it manually if you want this script to manage it."
        continue
    else
        ln -s "$plist_src" "$plist_dst"
        echo "Symlinked $plist_dst -> $plist_src"
        plist_changes=$((plist_changes + 1))
    fi

    # 3. Load the launchd agent if not already loaded
    if launchctl list "$label" &>/dev/null; then
        if (( plist_changes > 0 )); then
            # Plist changed — reload
            launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
            launchctl bootstrap "gui/$(id -u)" "$plist_dst"
            echo "Reloaded launchd agent $label (plist changed)"
        fi
    else
        launchctl bootstrap "gui/$(id -u)" "$plist_dst"
        echo "Loaded launchd agent $label"
        plist_changes=$((plist_changes + 1))
    fi

    changes=$((changes + plist_changes))
done

# 4. Remove the retired pre-split agent if it lingers
OLD_LABEL="com.natemccoy.clean-fix"
OLD_DST="$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
if launchctl list "$OLD_LABEL" &>/dev/null; then
    launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null || true
    echo "Unloaded retired agent $OLD_LABEL"
    changes=$((changes + 1))
fi
if [[ -L "$OLD_DST" || -e "$OLD_DST" ]]; then
    rm "$OLD_DST"
    echo "Removed retired plist $OLD_DST"
    changes=$((changes + 1))
fi

if (( changes == 0 )); then
    echo "Already set up — nothing to do."
else
    echo "Setup complete ($changes change(s))."
fi
