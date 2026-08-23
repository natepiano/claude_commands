#!/usr/bin/env bash

set -uo pipefail

# Make this repo's git clean filters work on whatever machine it is sitting on.
# Idempotent, quiet when everything is already in place, and safe to run from a
# hook. SessionStart runs it, so a fresh clone repairs itself on first launch.
#
# Why it has to exist: a clean filter is two halves in two places. .gitattributes
# names the driver and is committed, so it travels. The driver's definition
# lives in .git/config, which is never cloned. A clone with the name but no
# definition does NOT error -- git treats a missing driver as a plain
# pass-through, and filter.<name>.required does not cover it (that only applies
# to a defined filter that fails). The filters would simply be absent, and the
# churn they exist to hide would start landing in commits with nothing to say
# why. Nothing inside git can fix that: any config pointing git at a committed
# filter definition is itself a local config change, so the bootstrap has to
# come from outside the repo.
#
# Three things are needed, and all three are per-machine:
#   1. the filter drivers, in .git/config
#   2. the launchd watcher, which keeps `git status` quiet after each write
#   3. an index refresh right now, for changes made while any of this was missing

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=/dev/null
source "$SCRIPT_DIR/git_filters_table.sh"

if ! git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    echo "ensure_git_filters: $REPO_ROOT is not a git repository; skipping." >&2
    exit 0
fi

# ── 1. filter drivers ──
installed=()
for entry in "${GIT_FILTERS[@]}"; do
    name="${entry%%:*}"
    rest="${entry#*:}"
    script="${rest%%:*}"

    current="$(git -C "$REPO_ROOT" config --local --get "filter.$name.clean" 2>/dev/null || true)"
    [[ "$current" == "$script" ]] && continue

    if git -C "$REPO_ROOT" config --local "filter.$name.clean" "$script" 2>/dev/null &&
        git -C "$REPO_ROOT" config --local "filter.$name.smudge" cat 2>/dev/null &&
        git -C "$REPO_ROOT" config --local "filter.$name.required" true 2>/dev/null; then
        installed+=("$name")
    else
        # Worth saying out loud: an unwritable .git/config is exactly the state
        # this script exists to catch, and staying quiet about it would repeat
        # the failure it is here to prevent.
        echo "ensure_git_filters: could not configure filter.$name in $REPO_ROOT." >&2
    fi
done

# ── 2. launchd watcher ──
# The plist is generated rather than symlinked from a committed copy: it has to
# carry absolute paths, and a committed one would hard-code the home directory
# of the machine it was written on -- the exact assumption a new machine breaks.
ensure_watcher() {
    command -v launchctl >/dev/null 2>&1 || return 0

    # Only the live config directory gets a watcher. The label is a single
    # system-wide name, so any other copy of this repo -- a clone, a backup, a
    # worktree -- would otherwise point the one watcher at itself and silently
    # stop refreshing the directory that is actually in use. Filters still work
    # in such a copy; only the `git status` cosmetics lag, which is the right
    # trade for a checkout nothing is reading config from.
    local live_dir
    live_dir="$(cd "${CLAUDE_CONFIG_DIR:-$HOME/.claude}" 2>/dev/null && pwd)" || return 0
    [[ "$REPO_ROOT" == "$live_dir" ]] || return 0

    local dst="$HOME/Library/LaunchAgents/$GIT_FILTERS_LABEL.plist"
    local watch_paths="" entry rest desired
    for entry in "${GIT_FILTERS[@]}"; do
        rest="${entry#*:}"
        watch_paths+="        <string>$REPO_ROOT/${rest#*:}</string>"$'\n'
    done

    desired="$(
        cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$GIT_FILTERS_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$SCRIPT_DIR/refresh_filtered_index.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>WatchPaths</key>
    <array>
$watch_paths    </array>
    <key>ThrottleInterval</key>
    <integer>1</integer>
    <key>StandardOutPath</key>
    <string>/dev/null</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-settings-git-refresh.stderr.log</string>
</dict>
</plist>
PLIST
    )"

    # Reload only when the plist content or the loaded state actually changed;
    # a bootout/bootstrap cycle on every session start would be pure noise.
    if [[ -f "$dst" && ! -L "$dst" ]] && [[ "$(cat "$dst" 2>/dev/null)" == "$desired" ]] &&
        launchctl print "gui/$(id -u)/$GIT_FILTERS_LABEL" >/dev/null 2>&1; then
        return 0
    fi

    mkdir -p "$HOME/Library/LaunchAgents" || return 0
    rm -f "$dst"
    printf '%s\n' "$desired" > "$dst" || return 0

    launchctl bootout "gui/$(id -u)/$GIT_FILTERS_LABEL" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$dst" >/dev/null 2>&1 || true
    echo "ensure_git_filters: (re)loaded the $GIT_FILTERS_LABEL watcher"
}

ensure_watcher

# ── 3. catch up on anything that changed while the above was missing ──
git_filters_refresh_all "$REPO_ROOT"

if ((${#installed[@]} > 0)); then
    echo "ensure_git_filters: installed ${installed[*]} in $REPO_ROOT"
fi

exit 0
