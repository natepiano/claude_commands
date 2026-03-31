#!/bin/bash
# Lint watcher management script.
# Usage: status.sh [status|start|stop|restart]
#   status  — show current state (default)
#   start   — load and start the LaunchAgent
#   stop    — stop and unload the LaunchAgent
#   restart — stop then start

set -euo pipefail

RUST_DIR="$HOME/rust"
LABEL="com.natemccoy.lint-watcher"
PLIST="$HOME/Library/LaunchAgents/com.natemccoy.lint-watcher.plist"
CMD="${1:-status}"

do_stop() {
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl unload "$PLIST" 2>/dev/null
        echo "Stopped."
    else
        echo "Already stopped."
    fi
    # Kill any orphaned cargo-watch or run-lint processes
    pkill -f "cargo-watch.*run-lint" 2>/dev/null || true
    pkill -f "run-lint\.sh" 2>/dev/null || true
}

do_start() {
    if launchctl list "$LABEL" &>/dev/null; then
        echo "Already running."
    else
        launchctl load "$PLIST" 2>/dev/null
        echo "Started."
    fi
}

do_status() {
    echo "=== Lint Watcher Status ==="
    echo ""

    # 1. LaunchAgent status
    if launchctl list "$LABEL" &>/dev/null; then
        pid=$(launchctl list "$LABEL" 2>/dev/null | grep '"PID"' | grep -oE '[0-9]+' || true)
        if [[ -n "$pid" ]]; then
            echo "Service: running (PID $pid)"
        else
            last_exit=$(launchctl list "$LABEL" 2>/dev/null | grep 'LastExitStatus' | grep -oE '[0-9]+' || true)
            echo "Service: loaded but not running (last exit: ${last_exit:-unknown})"
        fi
    else
        echo "Service: not loaded"
    fi

    # 2. cargo-watch processes
    watch_count=$(pgrep -f "cargo-watch.*run-lint" 2>/dev/null | wc -l | tr -d ' ')
    echo "Watchers: $watch_count cargo-watch processes"
    echo ""

    # 3. Currently linting (check for run-lint.sh processes, exclude cargo-watch)
    lint_pid=$(pgrep -f "bash.*run-lint\.sh" 2>/dev/null | head -1 || true)
    if [[ -n "$lint_pid" ]]; then
        lint_args=$(ps -o args= -p "$lint_pid" 2>/dev/null || true)
        project=$(echo "$lint_args" | sed -n "s|.*run-lint\.sh \($RUST_DIR/[^ ]*\).*|\1|p" || true)
        if [[ -n "$project" ]]; then
            echo "Currently linting: $(basename "$project")"
        else
            echo "Currently linting: yes (PID $lint_pid)"
        fi
    else
        echo "Currently linting: none"
    fi

    echo ""
    echo "=== Per-Project Status ==="
    echo ""

    # 4. Per-project port-report.log status
    printf "%-30s %-10s %-22s %-8s %s\n" "PROJECT" "STATUS" "TIMESTAMP" "AGE" "DURATION"
    printf "%-30s %-10s %-22s %-8s %s\n" "-------" "------" "---------" "---" "--------"

    for project_dir in "$RUST_DIR"/*/; do
        name=$(basename "$project_dir")
        log_file="$project_dir/target/port-report.log"

        [[ ! -f "$project_dir/Cargo.toml" ]] && continue

        if [[ ! -f "$log_file" ]]; then
            printf "%-30s %-10s %-22s %-8s %s\n" "$name" "—" "" "" ""
            continue
        fi

        last_line=$(tail -1 "$log_file" 2>/dev/null || true)
        if [[ -z "$last_line" ]]; then
            printf "%-30s %-10s %-22s %-8s %s\n" "$name" "—" "" "" ""
            continue
        fi

        raw_timestamp=$(echo "$last_line" | cut -f1)
        status=$(echo "$last_line" | cut -f2)
        # Strip T and timezone for display
        timestamp=$(echo "$raw_timestamp" | sed 's/T/ /;s/[-+][0-9][0-9]:[0-9][0-9]$//')

        # Compute age from file mtime
        file_mtime=$(stat -f '%m' "$log_file" 2>/dev/null || echo 0)
        now_epoch=$(date "+%s")
        age_secs=$(( now_epoch - file_mtime ))

        # Format age as human-readable, right-aligned to trailing unit
        if (( age_secs < 60 )); then
            age_str="$(printf '%5ds' "$age_secs")"
        elif (( age_secs < 3600 )); then
            age_str="$(printf '%2dm %2ds' "$(( age_secs / 60 ))" "$(( age_secs % 60 ))")"
        else
            age_str="$(printf '%2dh %2dm' "$(( age_secs / 3600 ))" "$(( (age_secs % 3600) / 60 ))")"
        fi

        # Compute duration from last started→outcome pair
        dur_str=""
        if [[ "$status" == "passed" || "$status" == "failed" ]]; then
            # Find the last "started" line before this outcome
            last_started=$(grep '	started$' "$log_file" | tail -1 | cut -f1)
            if [[ -n "$last_started" ]]; then
                # Strip colon from timezone for macOS date -j (e.g. -04:00 → -0400)
                s_tz="${last_started%:*}${last_started##*:}"
                e_tz="${raw_timestamp%:*}${raw_timestamp##*:}"
                s_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S%z" "$s_tz" "+%s" 2>/dev/null || echo 0)
                e_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S%z" "$e_tz" "+%s" 2>/dev/null || echo 0)
                if (( s_epoch > 0 && e_epoch > 0 )); then
                    dur_secs=$(( e_epoch - s_epoch ))
                    if (( dur_secs < 60 )); then
                        dur_str="${dur_secs}s"
                    else
                        dur_str="$(( dur_secs / 60 ))m $(( dur_secs % 60 ))s"
                    fi
                fi
            fi
        fi

        # Check for stale "started"
        if [[ "$status" == "started" ]]; then
            if (( age_secs > 1800 )); then
                status="stale"
            else
                status="running"
            fi
        fi

        printf "%-30s %-10s %-22s %-8s %s\n" "$name" "$status" "$timestamp" "$age_str" "$dur_str"
    done
}

case "$CMD" in
    status)  do_status ;;
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; sleep 1; do_start ;;
    *)       echo "Usage: status.sh [status|start|stop|restart]"; exit 1 ;;
esac
