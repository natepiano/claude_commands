#!/bin/bash
# Check Port Report cache and return actionable results.
# Called by the /clippy command as a single step.
#
# Exit codes:
#   0 = cache hit (fresh results available)
#   1 = cache miss (agent should run mend + clippy)
#
# Output format (on exit 0):
#   Line 1: "cached: <timestamp>"
#   Lines 2-4: status table (cargo mend, cargo +nightly fmt, clippy)
#   If passed: Line 5 is git diff status ("clean" or "has changes")
#     If has changes: "=== git diff ===" followed by diff output
#   If failed: "=== cargo mend ===" and/or "=== cargo clippy ===" with details
#
# Usage: check_cache.sh [project_dir]
#   project_dir defaults to current directory

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../lint-watcher/cache-root.sh"

PROJECT_DIR="$(cd "${1:-.}" && pwd -P)"
TEMP_ROOT="$(cache_root)/port-report"
STALE_SECONDS=1800  # 30 minutes

project_key() {
    printf '%s' "$1" | od -An -tx1 | tr -d ' \n'
}

STATE_DIR="$TEMP_ROOT/$(project_key "$PROJECT_DIR")"
LATEST_FILE="$STATE_DIR/latest.json"
OUTPUT_DIR="$STATE_DIR/port-report"

if [[ ! -f "$LATEST_FILE" ]]; then
    echo "No latest.json found." >&2
    exit 1
fi

read_json_field() {
    local key="$1"
    python3 - "$LATEST_FILE" "$key" <<'PY2'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
try:
    data = json.loads(path.read_text())
except Exception:
    print("")
    raise SystemExit(0)
value = data.get(key)
if value is None:
    print("")
elif isinstance(value, str):
    print(value)
else:
    print(value)
PY2
}

parse_timestamp_epoch() {
    local timestamp="$1"
    python3 - "$timestamp" <<'PY2'
from datetime import datetime
import sys

value = sys.argv[1].strip()
if not value:
    print(0)
    raise SystemExit(0)

try:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
except ValueError:
    print(0)
    raise SystemExit(0)

print(int(dt.timestamp()))
PY2
}

raw_timestamp="$(read_json_field started_at)"
status="$(read_json_field status)"
finished_at="$(read_json_field finished_at)"

if [[ -z "$raw_timestamp" || -z "$status" ]]; then
    echo "latest.json is missing required fields." >&2
    exit 1
fi

if [[ "$status" == "running" ]]; then
    start_epoch=$(parse_timestamp_epoch "$raw_timestamp")
    now_epoch=$(date "+%s")
    age=$(( now_epoch - start_epoch ))
    if (( start_epoch == 0 || age > STALE_SECONDS )); then
        echo "Port Report run is stale (${age}s ago)." >&2
        exit 1
    fi

    echo "Port Report is running, waiting for results..." >&2
    timeout=300
    elapsed=0
    while (( elapsed < timeout )); do
        sleep 2
        elapsed=$(( elapsed + 2 ))
        raw_timestamp="$(read_json_field started_at)"
        status="$(read_json_field status)"
        finished_at="$(read_json_field finished_at)"
        if [[ "$status" != "running" ]]; then
            break
        fi
    done

    if [[ "$status" == "running" ]]; then
        echo "Timed out waiting for Port Report (${timeout}s)." >&2
        exit 1
    fi
fi

fresh_timestamp="$finished_at"
if [[ -z "$fresh_timestamp" ]]; then
    fresh_timestamp="$raw_timestamp"
fi

log_epoch=$(parse_timestamp_epoch "$fresh_timestamp")

if (( log_epoch == 0 )); then
    echo "Could not parse timestamp: $fresh_timestamp" >&2
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
    echo "Cache stale — source files changed after last Port Report." >&2
    exit 1
fi

case "$status" in
    passed|failed) ;;
    *)
        echo "Unsupported Port Report status: $status" >&2
        exit 1
        ;;
esac

display_timestamp=$(echo "$fresh_timestamp" | sed 's/T/ /;s/[-+][0-9][0-9]:[0-9][0-9]$//')

if [[ "$status" == "passed" ]]; then
    diff_output=$(git -C "$PROJECT_DIR" diff 2>/dev/null)
    echo "cached: $display_timestamp"
    echo "cargo mend        : passed"
    echo "cargo +nightly fmt: passed"
    echo "clippy            : passed"
    if [[ -z "$diff_output" ]]; then
        echo "git diff          : clean"
    else
        echo "git diff          : has changes"
        echo "=== git diff ==="
        echo "$diff_output"
    fi
fi

if [[ "$status" == "failed" ]]; then
    echo "cached: $display_timestamp"
    # Determine mend status
    mend_has_issues=false
    if [[ -f "$OUTPUT_DIR/mend-latest.log" ]]; then
        mend_output=$(cat "$OUTPUT_DIR/mend-latest.log")
        if [[ -n "$mend_output" ]] && ! echo "$mend_output" | grep -q "No findings"; then
            mend_has_issues=true
        fi
    fi

    # Determine clippy status
    clippy_has_issues=false
    if [[ -f "$OUTPUT_DIR/clippy-latest.log" ]]; then
        clippy_output=$(cat "$OUTPUT_DIR/clippy-latest.log")
        if [[ -n "$clippy_output" ]]; then
            clippy_has_issues=true
        fi
    fi

    if [[ "$mend_has_issues" == true ]]; then
        echo "cargo mend        : issues found"
    else
        echo "cargo mend        : passed"
    fi
    echo "cargo +nightly fmt: unknown (not cached)"
    if [[ "$clippy_has_issues" == true ]]; then
        echo "clippy            : issues found"
    else
        echo "clippy            : passed"
    fi

    # Output details
    if [[ "$mend_has_issues" == true ]]; then
        echo "=== cargo mend ==="
        cat "$OUTPUT_DIR/mend-latest.log"
    fi
    if [[ "$clippy_has_issues" == true ]]; then
        echo "=== cargo clippy ==="
        cat "$OUTPUT_DIR/clippy-latest.log"
    fi
fi
