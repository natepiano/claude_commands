#!/usr/bin/env bash

set -euo pipefail

LABEL="com.natemccoy.hanadocs-prioritize"
# Every python3 here goes through the repo shim, which picks an interpreter
# by VERSION rather than by path. These scripts used to pin "$PY" --
# Apple 3.9 on the Mac, nonexistent on NixOS. The pin was never load-bearing:
# the tests in tests/ import typing.override and cannot run under 3.9 at all.
PY="$HOME/.claude/scripts/lib/py"
# This script installs a launchd .plist, which only means anything on macOS.
# On NixOS the same daemon is declared in /etc/nixos/modules/hanadocs-prioritize.nix
# and installed by `rebuild`, so exit cleanly and say where to look rather than
# failing partway through on a missing launchctl.
if ! command -v launchctl >/dev/null 2>&1; then
    echo "launchctl not present: this is macOS-only."
    echo "On NixOS the watcher is a systemd user unit declared in"
    echo "  /etc/nixos/modules/hanadocs-prioritize.nix"
    echo "Install or restart it with: rebuild, or"
    echo "  systemctl --user restart hanadocs-prioritize.service"
    exit 0
fi

SOURCE_PLIST="$HOME/.claude/scripts/prioritize/com.natemccoy.hanadocs-prioritize.plist"
INSTALLED_PLIST="$HOME/Library/LaunchAgents/com.natemccoy.hanadocs-prioritize.plist"
RUNNER="$HOME/.claude/scripts/prioritize/run_watcher.sh"
SNAPSHOT_TOOL="$HOME/.claude/scripts/prioritize/snapshot.py"
RENUMBER_TOOL="$HOME/.claude/scripts/prioritize/renumber.py"
WRITER_LOCK_TOOL="$HOME/.claude/scripts/prioritize/writer_lock.py"
RUNNER_LOCK_TOOL="$HOME/.claude/scripts/prioritize/runner_lock.py"
SIGNATURE_TOOL="$HOME/.claude/scripts/prioritize/watch_signature.py"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/$([ "$(uname -s)" = Darwin ] && echo Library/Caches || echo .cache)}/hanadocs-prioritize"
STATE_DIR="/tmp/hanadocs-prioritize"
LAST_STATUS_FILE="$STATE_DIR/last-status"
EVENT_LOG="$STATE_DIR/events.log"
DOMAIN="gui/$(id -u)"
INITIAL_PASS_ATTEMPTS=120
created_symlink=0
bootstrap_started=0
install_succeeded=0
preflight_snapshot=""

cleanup() {
    local exit_status=$?
    set +e
    [[ -n "$preflight_snapshot" ]] && rm -f "$preflight_snapshot"
    if (( install_succeeded == 0 )); then
        if (( bootstrap_started == 1 )); then
            launchctl bootout "$DOMAIN" "$INSTALLED_PLIST" >/dev/null 2>&1
            if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
                echo "CRITICAL: failed watcher installation is still loaded; run launchctl bootout manually." >&2
            else
                echo "Rolled back the failed launchd installation." >&2
            fi
        fi
        if (( created_symlink == 1 )) && [[ -L "$INSTALLED_PLIST" ]]; then
            current_target="$(readlink "$INSTALLED_PLIST")"
            if [[ "$current_target" == "$SOURCE_PLIST" ]]; then
                rm -f "$INSTALLED_PLIST"
            fi
        fi
    fi
    trap - EXIT
    exit "$exit_status"
}

trap cleanup EXIT

if [[ "${1:-}" != "--install" ]] || (( $# != 1 )); then
    echo "Usage: $0 --install" >&2
    echo "The watcher ranks valid open issues and leaves incomplete issues unranked." >&2
    exit 2
fi

for required_file in "$SOURCE_PLIST" "$RUNNER" "$SNAPSHOT_TOOL" "$RENUMBER_TOOL" "$WRITER_LOCK_TOOL" "$RUNNER_LOCK_TOOL" "$SIGNATURE_TOOL"; do
    if [[ ! -f "$required_file" ]]; then
        echo "Missing required file: $required_file" >&2
        exit 1
    fi
done

/usr/bin/plutil -lint "$SOURCE_PLIST"
mkdir -p \
    "$HOME/Library/LaunchAgents" \
    "$CACHE_DIR" \
    "$STATE_DIR"

preflight_snapshot="$(mktemp "$CACHE_DIR/.install-preflight.XXXXXX")"
if ! "$PY" "$SNAPSHOT_TOOL" \
    --output "$preflight_snapshot"; then
    echo "Refusing to install: ranking inputs could not be snapshotted safely." >&2
    exit 1
fi

if ! "$PY" "$RENUMBER_TOOL" --check; then
    echo "Refusing to install: the currently valid subset is not mechanically canonical." >&2
    echo "Run renumber.py --apply, then retry installation." >&2
    exit 1
fi

if [[ -L "$INSTALLED_PLIST" ]]; then
    current_target="$(readlink "$INSTALLED_PLIST")"
    if [[ "$current_target" != "$SOURCE_PLIST" ]]; then
        echo "Refusing to replace unexpected symlink: $INSTALLED_PLIST -> $current_target" >&2
        exit 1
    fi
elif [[ -e "$INSTALLED_PLIST" ]]; then
    echo "Refusing to replace unmanaged file: $INSTALLED_PLIST" >&2
    exit 1
else
    ln -s "$SOURCE_PLIST" "$INSTALLED_PLIST"
    created_symlink=1
fi

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN" "$INSTALLED_PLIST"
fi
rm -f "$LAST_STATUS_FILE"
bootstrap_started=1
launchctl bootstrap "$DOMAIN" "$INSTALLED_PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "Watcher installation could not be verified." >&2
    exit 1
fi

watcher_result=""
for ((_attempt = 0; _attempt < INITIAL_PASS_ATTEMPTS; _attempt++)); do
    if [[ -f "$LAST_STATUS_FILE" ]]; then
        watcher_result="$(awk '{print $1; exit}' "$LAST_STATUS_FILE")"
        if [[ "$watcher_result" == "ok" || "$watcher_result" == "error" ]]; then
            break
        fi
    fi
    sleep 0.25
done

if [[ "$watcher_result" != "ok" ]]; then
    echo "Watcher loaded, but its initial ranking pass did not succeed." >&2
    [[ -f "$LAST_STATUS_FILE" ]] && cat "$LAST_STATUS_FILE" >&2
    [[ -f "$EVENT_LOG" ]] && tail -n 20 "$EVENT_LOG" >&2
    exit 1
fi

if ! "$PY" "$RENUMBER_TOOL" --check; then
    echo "Watcher started, but final valid-subset ranking validation failed." >&2
    exit 1
fi

install_succeeded=1
echo "Installed and started $LABEL"
echo "Valid open issues are ranked immediately; incomplete issues remain unranked."
echo "Use $HOME/.claude/scripts/prioritize/status_watcher.sh to inspect it."
