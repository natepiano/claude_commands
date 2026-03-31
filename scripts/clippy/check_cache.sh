#!/bin/bash
# Check lint watcher cache and return actionable results.
# Called by the /clippy command as a single step.
#
# Exit codes:
#   0 = cache hit (fresh results available)
#   1 = cache miss (agent should run mend + clippy)
#
# Output format (on exit 0):
#   Line 1: "passed" or "failed"
#   Line 2: timestamp of the result
#   If failed: remaining lines are the cached clippy + mend output
#
# Usage: check_cache.sh [project_dir]
#   project_dir defaults to current directory

set -euo pipefail

PROJECT_DIR="${1:-.}"
LOG_FILE="$PROJECT_DIR/target/port-report.log"
OUTPUT_DIR="$PROJECT_DIR/target/port-report"
STALE_SECONDS=1800  # 30 minutes

# --- No log file → cache miss
if [[ ! -f "$LOG_FILE" ]]; then
    echo "No port-report.log found." >&2
    exit 1
fi

last_line=$(tail -1 "$LOG_FILE")
if [[ -z "$last_line" ]]; then
    echo "port-report.log is empty." >&2
    exit 1
fi

raw_timestamp=$(echo "$last_line" | cut -f1)
status=$(echo "$last_line" | cut -f2)

# --- Currently running → wait for it
if [[ "$status" == "started" ]]; then
    # Check for stale
    file_mtime=$(stat -f '%m' "$LOG_FILE" 2>/dev/null || echo 0)
    now_epoch=$(date "+%s")
    age=$(( now_epoch - file_mtime ))
    if (( age > STALE_SECONDS )); then
        echo "Lint watcher started but stale (${age}s ago)." >&2
        exit 1
    fi

    echo "Lint watcher is running, waiting for results..." >&2
    timeout=300  # 5 minutes
    elapsed=0
    while (( elapsed < timeout )); do
        sleep 2
        elapsed=$(( elapsed + 2 ))
        last_line=$(tail -1 "$LOG_FILE")
        status=$(echo "$last_line" | cut -f2)
        if [[ "$status" != "started" ]]; then
            raw_timestamp=$(echo "$last_line" | cut -f1)
            break
        fi
    done

    if [[ "$status" == "started" ]]; then
        echo "Timed out waiting for lint watcher (${timeout}s)." >&2
        exit 1
    fi
fi

# --- Check freshness: compare log timestamp to newest source file
# Strip colon from timezone for macOS date -j (-04:00 → -0400)
tz_fixed="${raw_timestamp%:*}${raw_timestamp##*:}"
log_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S%z" "$tz_fixed" "+%s" 2>/dev/null || echo 0)

if (( log_epoch == 0 )); then
    echo "Could not parse timestamp: $raw_timestamp" >&2
    exit 1
fi

newest_source_mtime=$(
    rg --files -g '*.rs' -g '*.toml' "$PROJECT_DIR" 2>/dev/null |
    xargs stat -f '%m' 2>/dev/null |
    sort -rn |
    head -1
)

if [[ -z "$newest_source_mtime" ]]; then
    echo "No source files found." >&2
    exit 1
fi

if (( newest_source_mtime > log_epoch )); then
    echo "Cache stale — source files changed after last lint." >&2
    exit 1
fi

# --- Cache hit
display_timestamp=$(echo "$raw_timestamp" | sed 's/T/ /;s/[-+][0-9][0-9]:[0-9][0-9]$//')
echo "$status"
echo "$display_timestamp"

if [[ "$status" == "failed" ]]; then
    if [[ -f "$OUTPUT_DIR/mend-latest.log" ]]; then
        mend_output=$(cat "$OUTPUT_DIR/mend-latest.log")
        if [[ -n "$mend_output" ]] && ! echo "$mend_output" | grep -q "No findings"; then
            echo "=== cargo mend ==="
            cat "$OUTPUT_DIR/mend-latest.log"
        fi
    fi
    if [[ -f "$OUTPUT_DIR/clippy-latest.log" ]]; then
        echo "=== cargo clippy ==="
        cat "$OUTPUT_DIR/clippy-latest.log"
    fi
fi
