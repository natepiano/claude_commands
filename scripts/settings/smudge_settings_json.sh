#!/usr/bin/env bash

set -uo pipefail

# Git smudge filter for settings.json: merge the local-only keys back in from
# the sidecar whenever git writes the file. With no sidecar, or on any error,
# this behaves as a plain cat -- the filter is marked required, so it must
# never fail a checkout.
#
# The blob is buffered in a variable rather than a temp file: the filter runs
# in whatever environment git was invoked from, and a temp directory is not
# guaranteed there. The committed blob is jq output, so it always ends in one
# newline, which is what the fallback prints back.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/settings_local_keys.sh"

sidecar="$REPO_ROOT/$SETTINGS_LOCAL_KEYS_SIDECAR"
blob="$(cat)"

if [[ -s "$sidecar" ]]; then
    merged="$(printf '%s\n' "$blob" |
        jq --slurpfile local "$sidecar" "$SETTINGS_LOCAL_KEYS_MERGE" 2>/dev/null)" &&
        [[ -n "$merged" ]] &&
        { printf '%s\n' "$merged"; exit 0; }
fi

printf '%s\n' "$blob"
