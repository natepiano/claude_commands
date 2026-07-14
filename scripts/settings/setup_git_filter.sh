#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required for the Claude settings Git filter." >&2
  exit 1
fi

git -C "$REPO_ROOT" config --local \
  filter.claude-settings.clean scripts/settings/clean_settings_json.sh
git -C "$REPO_ROOT" config --local filter.claude-settings.smudge cat
git -C "$REPO_ROOT" config --local filter.claude-settings.required true

echo "Configured the Claude settings Git filter for $REPO_ROOT"
