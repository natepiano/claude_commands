#!/usr/bin/env bash

set -euo pipefail

# Git clean filter for settings.json: the local-only keys and hook groups never
# reach a commit.
# The key list, and the smudge that puts them back, live in
# settings_local_keys.sh.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/settings_local_keys.sh"

jq --argjson keys "$SETTINGS_LOCAL_KEYS_JSON" "$SETTINGS_LOCAL_KEYS_STRIP"
