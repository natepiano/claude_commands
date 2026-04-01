#!/bin/bash
# Per-project lint runner. Called by cargo-watch when .rs or Cargo.toml changes.
# Acquires a global lock so only one project lints at a time.
# Writes status to a temp-rooted port-report.log, raw output to a temp-rooted
# port-report/ directory.
#
# Usage: run-lint.sh <project_dir>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/cache-root.sh"

PROJECT_DIR="$(cd "$1" && pwd -P)"
TEMP_ROOT="$(cache_root)/port-report"

project_key() {
    printf '%s' "$1" | od -An -tx1 | tr -d ' \n'
}

PROJECT_STATE_DIR="$TEMP_ROOT/$(project_key "$PROJECT_DIR")"
LOG_FILE="$PROJECT_STATE_DIR/port-report.log"
OUTPUT_DIR="$PROJECT_STATE_DIR/port-report"

mkdir -p "$OUTPUT_DIR"

timestamp() {
    date -Iseconds
}

echo "$(timestamp)	started" >> "$LOG_FILE"

status="passed"

# Run cargo mend, capture output
if ! nice -n 10 cargo mend --manifest-path "$PROJECT_DIR/Cargo.toml" \
    > "$OUTPUT_DIR/mend-latest.log.tmp" 2>&1; then
    status="failed"
fi
mv "$OUTPUT_DIR/mend-latest.log.tmp" "$OUTPUT_DIR/mend-latest.log"

# Run cargo clippy, capture output
if ! nice -n 10 cargo clippy --workspace --all-targets --all-features \
    --manifest-path "$PROJECT_DIR/Cargo.toml" -- -D warnings \
    > "$OUTPUT_DIR/clippy-latest.log.tmp" 2>&1; then
    status="failed"
fi
mv "$OUTPUT_DIR/clippy-latest.log.tmp" "$OUTPUT_DIR/clippy-latest.log"

echo "$(timestamp)	$status" >> "$LOG_FILE"
