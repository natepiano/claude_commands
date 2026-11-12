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

# Read the filter's repository once for membership and object order.
committed="$(git -C "$_SETTINGS_LOCAL_ROOT" show HEAD:settings.json 2>/dev/null |
    jq -cs 'if length == 1 and (.[0] | type) == "object" then .[0] else null end' 2>/dev/null)" || committed=null
# shellcheck disable=SC2016
baseline="$(jq --argjson committed "$committed" 'if $committed == null then keys else $committed | keys end' "$input")" || exit 1
sidecar="$_SETTINGS_LOCAL_ROOT/$SETTINGS_LOCAL_KEYS_SIDECAR"
if [[ "$committed" == null && -s "$sidecar" ]]; then
    # Keep previously saved local keys local when HEAD is unavailable.
    # shellcheck disable=SC2016
    baseline="$(jq --argjson baseline "$baseline" '$baseline - keys' "$sidecar")" || exit 1
fi

# Keep snapshot errors even when Git discards filter stderr.
log="$_SETTINGS_LOCAL_ROOT/settings.local-keys.errors.log"
if ! errors="$(settings_local_keys_snapshot "$input" "$baseline" 2>&1)"; then
    printf '%s\n' "$errors" >>"$log"
    echo "settings: cannot preserve local settings values; see $log" >&2
    exit 1
fi
jq --argjson baseline "$baseline" --argjson committed "$committed" "$SETTINGS_LOCAL_KEYS_STRIP" "$input"
