#!/usr/bin/env bash

set -euo pipefail

# Git clean filter for settings.json: the local-only keys and hook groups never
# reach a commit.
# The key list, and the smudge that puts them back, live in
# settings_local_keys.sh.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/settings_local_keys.sh"

input="$(mktemp)" || exit 1
trap 'rm -f "$input"' EXIT
cat >"$input" || exit 1

# Keep snapshot errors even when Git discards filter stderr.
log="$_SETTINGS_LOCAL_ROOT/settings.local-keys.errors.log"
if ! errors="$(settings_local_keys_snapshot "$input" 2>&1)"; then
    printf '%s\n' "$errors" >>"$log"
    echo "settings: cannot preserve local settings values; see $log" >&2
    exit 1
fi
baseline="$(settings_local_keys_baseline)" || exit 1
jq --argjson baseline "$baseline" "$SETTINGS_LOCAL_KEYS_STRIP" "$input"
