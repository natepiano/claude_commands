#!/bin/bash

set -uo pipefail

WATCH_DIR="$HOME/Library/Logs/claude-background-watch"
EVENT_LOG="$WATCH_DIR/events.log"
RUN_LOG="$WATCH_DIR/watch.log"
KILL_MODE_FILE="$WATCH_DIR/kill-enabled"
SEEN_FILE="$WATCH_DIR/seen-pids.txt"
CLAUDE_BIN="$HOME/.local/bin/claude"
CLAUDE_VERSION_DIR="$HOME/.local/share/claude/versions/"

mkdir -p "$WATCH_DIR"
touch "$EVENT_LOG" "$RUN_LOG" "$SEEN_FILE"

log_run() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$RUN_LOG"
}

log_event() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$EVENT_LOG"
}

notify_user() {
    local message="$1"
    local escaped_message
    escaped_message="${message//\\/\\\\}"
    escaped_message="${escaped_message//\"/\\\"}"
    /usr/bin/osascript -e "display notification \"${escaped_message}\" with title \"Claude Background Watch\"" >/dev/null 2>&1 || true
}

matches_background_claude() {
    local pid="$1"
    local tty="$2"
    local args="$3"

    [[ "$tty" == "?" || "$tty" == "??" ]] || return 1
    [[ "$args" == *"claude-background-watch"* ]] && return 1
    [[ "$args" == *"--remote-control"* ]] && return 1

    # Skip subagents and nightly jobs by walking the ancestor tree
    local check_pid="$pid"
    for _ in 1 2 3 4 5 6; do
        check_pid="$(ps -p "$check_pid" -o ppid= 2>/dev/null | tr -d ' ')" || break
        [[ -n "$check_pid" && "$check_pid" != "1" ]] || break
        local ancestor_tty ancestor_args
        ancestor_tty="$(ps -p "$check_pid" -o tty= 2>/dev/null | tr -d ' ')" || continue
        # If any ancestor has a TTY, it's a child of an interactive session
        [[ "$ancestor_tty" == "??" || "$ancestor_tty" == "?" || -z "$ancestor_tty" ]] || return 1
        # If any ancestor is the nightly job, skip it
        ancestor_args="$(ps -p "$check_pid" -o args= 2>/dev/null)" || continue
        [[ "$ancestor_args" == *"nightly-rust-clean-build"* ]] && return 1
    done

    [[ "$args" == *"$CLAUDE_BIN"* ]] && return 0
    [[ "$args" == *"$CLAUDE_VERSION_DIR"* ]] && return 0
    [[ "$args" == "claude"* ]] && return 0

    return 1
}

snapshot_process() {
    local pid="$1"
    local ppid="$2"
    local tty="$3"
    local args="$4"
    local stamp file gppid

    stamp="$(date '+%Y-%m-%dT%H-%M-%S')"
    file="$WATCH_DIR/incident-${stamp}-pid${pid}.log"
    gppid="$(ps -p "$ppid" -o ppid= 2>/dev/null | tr -d ' ' || true)"

    {
        printf 'timestamp=%s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
        printf 'pid=%s\n' "$pid"
        printf 'ppid=%s\n' "$ppid"
        printf 'tty=%s\n' "$tty"
        printf 'args=%s\n' "$args"
        printf 'kill_mode=%s\n' "$([[ -f "$KILL_MODE_FILE" ]] && echo enabled || echo disabled)"
        printf '\n[process]\n'
        ps -p "$pid" -o pid=,ppid=,tty=,etime=,command= 2>/dev/null || true
        printf '\n[parent]\n'
        ps -p "$ppid" -o pid=,ppid=,tty=,etime=,command= 2>/dev/null || true
        if [[ -n "$gppid" ]]; then
            printf '\n[grandparent]\n'
            ps -p "$gppid" -o pid=,ppid=,tty=,etime=,command= 2>/dev/null || true
        fi
        printf '\n[claude processes]\n'
        ps -axo pid=,ppid=,tty=,etime=,command= | grep -i claude | grep -v grep || true
        printf '\n[launch agents]\n'
        launchctl list | grep -Ei 'claude|anthropic|nightly' || true
    } > "$file"

    log_event "caught pid=$pid ppid=$ppid tty=$tty incident=$file"

    if [[ -f "$KILL_MODE_FILE" ]]; then
        kill "$pid" >/dev/null 2>&1 || true
        printf '\n[action]\nSIGTERM sent to pid %s\n' "$pid" >> "$file"
        log_event "sent_sigterm pid=$pid"
        notify_user "Stopped background Claude process $pid. Incident: $(basename "$file")"
    else
        notify_user "Caught background Claude process $pid. Incident: $(basename "$file")"
    fi
}

log_run "watcher starting"

while true; do
    current_file="$WATCH_DIR/current-pids.txt"
    : > "$current_file"

    while read -r pid ppid tty args; do
        [[ -n "${pid:-}" ]] || continue

        if matches_background_claude "$pid" "$tty" "$args"; then
            printf '%s\n' "$pid" >> "$current_file"
            if ! grep -qx "$pid" "$SEEN_FILE" 2>/dev/null; then
                snapshot_process "$pid" "$ppid" "$tty" "$args"
            fi
        fi
    done < <(ps -axo pid=,ppid=,tty=,command=)

    mv "$current_file" "$SEEN_FILE"

    sleep 0.25
done
