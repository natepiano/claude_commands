#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LABEL="com.natemccoy.claude-settings-git-refresh"
PLIST_SRC="$SCRIPT_DIR/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required for the Claude settings Git filter." >&2
  exit 1
fi

git -C "$REPO_ROOT" config --local \
  filter.claude-settings.clean scripts/settings/clean_settings_json.sh
git -C "$REPO_ROOT" config --local filter.claude-settings.smudge cat
git -C "$REPO_ROOT" config --local filter.claude-settings.required true

mkdir -p "$HOME/Library/LaunchAgents"
if [[ -e "$PLIST_DST" && ! -L "$PLIST_DST" ]]; then
  echo "ERROR: $PLIST_DST exists and is not a symlink." >&2
  exit 1
fi
ln -sfn "$PLIST_SRC" "$PLIST_DST"

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
fi
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Configured the Claude settings Git filter and watcher for $REPO_ROOT"
