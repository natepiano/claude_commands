#!/bin/bash
# Per-project lint runner. Called by cargo-watch when .rs or Cargo.toml changes.
# Acquires a global lock so only one project lints at a time.
# Writes status to target/port-report.log, raw output to target/port-report/.
#
# Usage: run-lint.sh <project_dir>

set -euo pipefail

PROJECT_DIR="$1"
LOG_FILE="$PROJECT_DIR/target/port-report.log"
OUTPUT_DIR="$PROJECT_DIR/target/port-report"

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
