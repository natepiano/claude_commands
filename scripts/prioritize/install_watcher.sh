#!/bin/bash

set -euo pipefail

LABEL="com.natemccoy.hanadocs-prioritize"
SOURCE_PLIST="/Users/natemccoy/.claude/scripts/prioritize/com.natemccoy.hanadocs-prioritize.plist"
INSTALLED_PLIST="/Users/natemccoy/Library/LaunchAgents/com.natemccoy.hanadocs-prioritize.plist"
RUNNER="/Users/natemccoy/.claude/scripts/prioritize/run_watcher.sh"
SNAPSHOT_TOOL="/Users/natemccoy/.claude/scripts/prioritize/snapshot.py"
RENUMBER_TOOL="/Users/natemccoy/.claude/scripts/prioritize/renumber.py"
WRITER_LOCK_TOOL="/Users/natemccoy/.claude/scripts/prioritize/writer_lock.py"
RUNNER_LOCK_TOOL="/Users/natemccoy/.claude/scripts/prioritize/runner_lock.py"
SIGNATURE_TOOL="/Users/natemccoy/.claude/scripts/prioritize/watch_signature.py"
CACHE_DIR="/Users/natemccoy/Library/Caches/hanadocs-prioritize"
STATE_DIR="/tmp/hanadocs-prioritize"
LAST_STATUS_FILE="$STATE_DIR/last-status"
EVENT_LOG="$STATE_DIR/events.log"
DOMAIN="gui/$(/usr/bin/id -u)"
INITIAL_PASS_ATTEMPTS=120
created_symlink=0
bootstrap_started=0
install_succeeded=0
preflight_snapshot=""

cleanup() {
    local exit_status=$?
    set +e
    [[ -n "$preflight_snapshot" ]] && /bin/rm -f "$preflight_snapshot"
    if (( install_succeeded == 0 )); then
        if (( bootstrap_started == 1 )); then
            /bin/launchctl bootout "$DOMAIN" "$INSTALLED_PLIST" >/dev/null 2>&1
            if /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
                echo "CRITICAL: failed watcher installation is still loaded; run launchctl bootout manually." >&2
            else
                echo "Rolled back the failed launchd installation." >&2
            fi
        fi
        if (( created_symlink == 1 )) && [[ -L "$INSTALLED_PLIST" ]]; then
            current_target="$(/usr/bin/readlink "$INSTALLED_PLIST")"
            if [[ "$current_target" == "$SOURCE_PLIST" ]]; then
                /bin/rm -f "$INSTALLED_PLIST"
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
/bin/mkdir -p \
    "/Users/natemccoy/Library/LaunchAgents" \
    "$CACHE_DIR" \
    "$STATE_DIR"

preflight_snapshot="$(/usr/bin/mktemp "$CACHE_DIR/.install-preflight.XXXXXX")"
if ! /usr/bin/python3 "$SNAPSHOT_TOOL" \
    --output "$preflight_snapshot"; then
    echo "Refusing to install: ranking inputs could not be snapshotted safely." >&2
    exit 1
fi

if ! /usr/bin/python3 "$RENUMBER_TOOL" --check; then
    echo "Refusing to install: the currently valid subset is not mechanically canonical." >&2
    echo "Run renumber.py --apply, then retry installation." >&2
    exit 1
fi

if [[ -L "$INSTALLED_PLIST" ]]; then
    current_target="$(/usr/bin/readlink "$INSTALLED_PLIST")"
    if [[ "$current_target" != "$SOURCE_PLIST" ]]; then
        echo "Refusing to replace unexpected symlink: $INSTALLED_PLIST -> $current_target" >&2
        exit 1
    fi
elif [[ -e "$INSTALLED_PLIST" ]]; then
    echo "Refusing to replace unmanaged file: $INSTALLED_PLIST" >&2
    exit 1
else
    /bin/ln -s "$SOURCE_PLIST" "$INSTALLED_PLIST"
    created_symlink=1
fi

if /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    /bin/launchctl bootout "$DOMAIN" "$INSTALLED_PLIST"
fi
/bin/rm -f "$LAST_STATUS_FILE"
bootstrap_started=1
/bin/launchctl bootstrap "$DOMAIN" "$INSTALLED_PLIST"
/bin/launchctl kickstart -k "$DOMAIN/$LABEL"

if ! /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "Watcher installation could not be verified." >&2
    exit 1
fi

watcher_result=""
for ((_attempt = 0; _attempt < INITIAL_PASS_ATTEMPTS; _attempt++)); do
    if [[ -f "$LAST_STATUS_FILE" ]]; then
        watcher_result="$(/usr/bin/awk '{print $1; exit}' "$LAST_STATUS_FILE")"
        if [[ "$watcher_result" == "ok" || "$watcher_result" == "error" ]]; then
            break
        fi
    fi
    /bin/sleep 0.25
done

if [[ "$watcher_result" != "ok" ]]; then
    echo "Watcher loaded, but its initial ranking pass did not succeed." >&2
    [[ -f "$LAST_STATUS_FILE" ]] && /bin/cat "$LAST_STATUS_FILE" >&2
    [[ -f "$EVENT_LOG" ]] && /usr/bin/tail -n 20 "$EVENT_LOG" >&2
    exit 1
fi

if ! /usr/bin/python3 "$RENUMBER_TOOL" --check; then
    echo "Watcher started, but final valid-subset ranking validation failed." >&2
    exit 1
fi

install_succeeded=1
echo "Installed and started $LABEL"
echo "Valid open issues are ranked immediately; incomplete issues remain unranked."
echo "Use /Users/natemccoy/.claude/scripts/prioritize/status_watcher.sh to inspect it."
