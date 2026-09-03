#!/usr/bin/env bash

set -u

LABEL="com.natemccoy.hanadocs-prioritize"
# Every python3 here goes through the repo shim, which picks an interpreter
# by VERSION rather than by path. These scripts used to pin "$PY" --
# Apple 3.9 on the Mac, nonexistent on NixOS. The pin was never load-bearing:
# the tests in tests/ import typing.override and cannot run under 3.9 at all.
PY="$HOME/.claude/scripts/lib/py"
INSTALLED_PLIST="$HOME/Library/LaunchAgents/com.natemccoy.hanadocs-prioritize.plist"
SOURCE_PLIST="$HOME/.claude/scripts/prioritize/com.natemccoy.hanadocs-prioritize.plist"
STATE_DIR="/tmp/hanadocs-prioritize"
LAST_STATUS_FILE="$STATE_DIR/last-status"
EVENT_LOG="$STATE_DIR/events.log"
RUNNER_LOCK_FILE="$STATE_DIR/runner.lock"
RUNNER_LOCK_TOOL="$HOME/.claude/scripts/prioritize/runner_lock.py"
WRITER_LOCK_TOOL="$HOME/.claude/scripts/prioritize/writer_lock.py"
PENDING_FILE="$STATE_DIR/pending"
SUCCESS_SNAPSHOT="${XDG_CACHE_HOME:-${HOME}/Library/Caches}/hanadocs-prioritize/semantic-inputs.json"
DOMAIN="gui/$(id -u)"

if [[ -L "$INSTALLED_PLIST" ]]; then
    target="$(readlink "$INSTALLED_PLIST")"
    if [[ "$target" == "$SOURCE_PLIST" ]]; then
        echo "plist: installed (managed symlink)"
    else
        echo "plist: unexpected symlink -> $target"
    fi
elif [[ -e "$INSTALLED_PLIST" ]]; then
    echo "plist: installed (unmanaged file)"
else
    echo "plist: not installed"
fi

if /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "launchd: loaded"
else
    echo "launchd: not loaded"
fi

if [[ -f "$LAST_STATUS_FILE" ]]; then
    echo "last status: $(<"$LAST_STATUS_FILE")"
else
    echo "last status: unavailable"
fi

if [[ -f "$SUCCESS_SNAPSHOT" ]]; then
    echo "snapshot: $SUCCESS_SNAPSHOT"
else
    echo "snapshot: no successful run recorded"
fi

if [[ -f "$RUNNER_LOCK_TOOL" ]]; then
    runner_state="$("$PY" "$RUNNER_LOCK_TOOL" status "$RUNNER_LOCK_FILE" 2>/dev/null)"
    echo "runner lock: $runner_state"
else
    echo "runner lock: status tool missing"
fi
if [[ -f "$WRITER_LOCK_TOOL" ]]; then
    writer_state="$("$PY" "$WRITER_LOCK_TOOL" --status 2>/dev/null)"
    echo "writer lock: $writer_state"
else
    echo "writer lock: status tool missing"
fi
if [[ -e "$PENDING_FILE" ]]; then
    echo "pending rerun: yes"
else
    echo "pending rerun: no"
fi

if [[ -f "$EVENT_LOG" ]]; then
    echo
    echo "Recent events:"
    tail -n 20 "$EVENT_LOG"
else
    echo "events: no watcher log yet"
fi
