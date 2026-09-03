#!/usr/bin/env bash

set -uo pipefail

# launchd WatchPaths entry point: after a filtered file is written, refresh its
# index entry so `git status` stops reporting it as modified.
#
# git status does not run clean filters, so without this a filtered file reads
# as modified for as long as its working copy differs from its committed form
# -- which, for these files, is always. See git_filters_table.sh.

# launchd hands a job an almost-empty PATH, so this used to be pinned to the
# homebrew layout. That pinning is what broke it under systemd on NixOS, where
# git lives in the nix store and none of these directories exist. Keep whatever
# PATH the caller has and only ADD the usual locations, so the same script works
# under launchd, under systemd, and when run by hand.
export PATH="${PATH:+$PATH:}/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# launchd hands the job a pipe it does not read; drain it so nothing blocks.
cat >/dev/null 2>&1 || true

# shellcheck source=/dev/null
source "$SCRIPT_DIR/git_filters_table.sh"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/settings_local_keys.sh"

# Every write to settings.json is the moment to copy its local-only keys to
# the sidecar; the smudge filter puts them back the next time git writes the
# file. See settings_local_keys.sh.
settings_local_keys_snapshot "$REPO_ROOT"
git_filters_refresh_all "$REPO_ROOT"

exit 0
