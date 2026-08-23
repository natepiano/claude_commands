#!/usr/bin/env bash

set -uo pipefail

# launchd WatchPaths entry point: after a filtered file is written, refresh its
# index entry so `git status` stops reporting it as modified.
#
# git status does not run clean filters, so without this a filtered file reads
# as modified for as long as its working copy differs from its committed form
# -- which, for these files, is always. See git_filters_table.sh.

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# launchd hands the job a pipe it does not read; drain it so nothing blocks.
cat >/dev/null 2>&1 || true

# shellcheck source=/dev/null
source "$SCRIPT_DIR/git_filters_table.sh"

git_filters_refresh_all "$REPO_ROOT"

exit 0
