#!/usr/bin/env bash

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cat >/dev/null
clean_hash="$(
  "$SCRIPT_DIR/clean_settings_json.sh" < "$REPO_ROOT/settings.json" |
    git -C "$REPO_ROOT" hash-object --stdin
)"
index_entry="$(git -C "$REPO_ROOT" ls-files --stage -- settings.json)"
read -r index_mode index_hash _ <<<"$index_entry"

if [[ "$clean_hash" != "$index_hash" ]]; then
  exit 0
fi

git -C "$REPO_ROOT" update-index --refresh -- settings.json >/dev/null 2>&1 || true

refreshed_hash="$(git -C "$REPO_ROOT" rev-parse --verify :settings.json)"
if [[ "$refreshed_hash" != "$index_hash" ]]; then
  git -C "$REPO_ROOT" update-index --cacheinfo "$index_mode" "$index_hash" settings.json
  exit 1
fi
