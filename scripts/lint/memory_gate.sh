#!/usr/bin/env bash
# Shared memory admission for build steps and BRP launches.

BUILD_HOLD_MARK_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../build_hold" && pwd)/build_hold.py"

build_hold_mark() {
    [[ -n "${CLAUDE_CODE_SESSION_ID:-}" ]] || return 0
    local holder_dir="${BUILD_HOLD_DIR:-$HOME/.local/state/build-hold}" holder
    for holder in "$holder_dir"/* "$holder_dir"/.[!.]* "$holder_dir"/..?*; do
        [[ -f "$holder" ]] && break
    done
    [[ -f "$holder" ]] || return 0
    if [[ -n "${2:-}" ]]; then
        python3 "$BUILD_HOLD_MARK_SCRIPT" mark --session-id "$CLAUDE_CODE_SESSION_ID" --state "$1" --outcome "$2" >/dev/null 2>&1 || true
    else
        python3 "$BUILD_HOLD_MARK_SCRIPT" mark --session-id "$CLAUDE_CODE_SESSION_ID" --state "$1" >/dev/null 2>&1 || true
    fi
}

buildlog_wait_for_memory() {
    BUILDLOG_MEM_WAIT_S=0
    BUILDLOG_MEM_OUTCOME=MeminfoUnavailable
    local meminfo="${BUILDLOG_MEMINFO:-/proc/meminfo}"
    local line available_kb started announced=0 interval="${BUILDLOG_MEM_POLL_S:-5}" limit="${BUILDLOG_MEM_WAIT_LIMIT_S:-900}"
    [[ "$interval" =~ ^[0-9]+$ && "$interval" -gt 0 ]] || interval=5
    [[ "$limit" =~ ^[0-9]+$ && "$limit" -ge 0 ]] || limit=900
    while [[ -r "$meminfo" ]]; do
        available_kb=""
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" =~ ^MemAvailable:[[:space:]]+([0-9]+)[[:space:]]+kB[[:space:]]*$ ]]; then
                available_kb="${BASH_REMATCH[1]}"
                break
            fi
        done < "$meminfo" 2>/dev/null || break
        [[ -n "$available_kb" ]] || break
        if (( 10#$available_kb >= 12582912 )); then
            BUILDLOG_MEM_OUTCOME=Granted
            break
        fi
        if (( announced == 0 )); then
            started=$SECONDS
            awk -v free="$available_kb" -v since="$(TZ=America/Los_Angeles date '+%H:%M %Z')" \
                'BEGIN { printf "waiting for memory since %s: the machine has %.1f GiB free; a build starts at 12.0\n", since, free / 1048576 > "/dev/stderr" }'
            announced=1
            build_hold_mark WaitingForMemory
        fi
        if (( SECONDS - started >= limit )); then
            echo 'memory wait limit reached after 15 min; starting anyway' >&2
            BUILDLOG_MEM_OUTCOME=TimedOut
            break
        fi
        sleep "$interval"
    done
    if (( announced != 0 )); then
        BUILDLOG_MEM_WAIT_S=$(( SECONDS - started ))
    fi
}
