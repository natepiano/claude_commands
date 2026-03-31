#!/bin/bash
# Orchestrator: spawns a cargo-watch process per eligible Rust project.
# Each cargo-watch watches for .rs and Cargo.toml changes and runs run-lint.sh.
# Stays alive as a long-running process managed by launchd.
#
# Compatible with macOS bash 3.x (no associative arrays).
#
# Usage: lint-watcher.sh

set -euo pipefail

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:$PATH"
source "$HOME/.cargo/env" 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_DIR="$HOME/rust"
CONF_FILE="$HOME/.claude/scripts/nightly/nightly-rust.conf"
RUN_LINT="$SCRIPT_DIR/run-lint.sh"

# Parse exclude list from nightly conf
excludes=()
if [[ -f "$CONF_FILE" ]]; then
    current_section=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="${stripped## }"
        stripped="${stripped%% }"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            current_section="${BASH_REMATCH[1]}"
            continue
        fi
        [[ "$current_section" == "exclude" ]] && excludes+=("$stripped")
    done < "$CONF_FILE"
fi

is_excluded() {
    local name="$1"
    for exclude in "${excludes[@]}"; do
        [[ "$name" == "$exclude" ]] && return 0
    done
    return 1
}

# Build eligible project list
projects=()
for project_dir in "$RUST_DIR"/*/; do
    name=$(basename "$project_dir")

    # Skip non-Rust
    [[ ! -f "$project_dir/Cargo.toml" ]] && continue

    # Skip excluded
    is_excluded "$name" && continue

    projects+=("${project_dir%/}")
done

if [[ ${#projects[@]} -eq 0 ]]; then
    echo "No eligible projects found."
    exit 0
fi

echo "=== Lint watcher: ${#projects[@]} projects ==="

# Parallel arrays: project_dirs[i] <-> child_pids[i]
project_dirs=()
child_pids=()

cleanup() {
    echo "Shutting down lint watcher..."
    for pid in "${child_pids[@]}"; do
        # Kill grandchildren (run-lint.sh, cargo mend, cargo clippy) first
        pkill -P "$pid" 2>/dev/null || true
        # Then kill cargo-watch itself
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null
    echo "Lint watcher stopped."
    exit 0
}
trap cleanup SIGTERM SIGINT

# Spawn cargo-watch per project
for project_dir in "${projects[@]}"; do
    name=$(basename "$project_dir")
    cargo-watch \
        -w "$project_dir/src" \
        -w "$project_dir/Cargo.toml" \
        -s "$RUN_LINT $project_dir" \
        --no-vcs-ignores \
        -C "$project_dir" \
        --delay 2 \
        --postpone \
        &
    project_dirs+=("$project_dir")
    child_pids+=($!)
    echo "Watching: $name (PID $!)"
done

echo ""
echo "All watchers started. Waiting..."

# Monitor children — restart any that die unexpectedly
while true; do
    i=0
    while [[ $i -lt ${#project_dirs[@]} ]]; do
        pid="${child_pids[$i]}"
        if ! kill -0 "$pid" 2>/dev/null; then
            project_dir="${project_dirs[$i]}"
            name=$(basename "$project_dir")
            echo "Restarting watcher for $name (PID $pid died)"
            cargo-watch \
                -w "$project_dir/src" \
                -w "$project_dir/Cargo.toml" \
                -s "$RUN_LINT $project_dir" \
                --no-vcs-ignores \
                -C "$project_dir" \
                --delay 2 \
                &
            child_pids[$i]=$!
            echo "Restarted: $name (PID $!)"
        fi
        i=$((i + 1))
    done
    sleep 10
done
