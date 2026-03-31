#!/bin/bash
# Orchestrator: spawns a cargo-watch process per eligible Rust project.
# Each cargo-watch watches for .rs and Cargo.toml changes and runs run-lint.sh.
# Stays alive as a long-running process managed by launchd.
# Detects new projects appearing under ~/rust/ every 10 seconds.
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

is_watched() {
    local dir="$1"
    local i=0
    while [[ $i -lt ${#project_dirs[@]} ]]; do
        [[ "${project_dirs[$i]}" == "$dir" ]] && return 0
        i=$((i + 1))
    done
    return 1
}

# Spawn a cargo-watch for a project directory. Appends to project_dirs/child_pids.
spawn_watcher() {
    local project_dir="$1"
    local name
    name=$(basename "$project_dir")

    # Build -w flags for all directories containing .rs or .toml files
    local watch_flags=()
    while IFS= read -r dir; do
        watch_flags+=(-w "$dir")
    done < <(rg --files -g '*.rs' -g '*.toml' "$project_dir" 2>/dev/null | xargs -n1 dirname | sort -u)

    if [[ ${#watch_flags[@]} -eq 0 ]]; then
        echo "SKIP: $name (no .rs or .toml files found)"
        return 1
    fi

    cargo-watch \
        "${watch_flags[@]}" \
        -s "$RUN_LINT $project_dir" \
        -C "$project_dir" \
        --delay 2 \
        &
    project_dirs+=("$project_dir")
    child_pids+=($!)
    echo "Watching: $name (PID $!)"
}

# Parallel arrays: project_dirs[i] <-> child_pids[i]
project_dirs=()
child_pids=()

cleanup() {
    echo "Shutting down lint watcher..."
    for pid in "${child_pids[@]}"; do
        pkill -P "$pid" 2>/dev/null || true
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null
    echo "Lint watcher stopped."
    exit 0
}
trap cleanup SIGTERM SIGINT

# Initial scan
for project_dir in "$RUST_DIR"/*/; do
    name=$(basename "$project_dir")
    [[ ! -f "$project_dir/Cargo.toml" ]] && continue
    is_excluded "$name" && continue
    spawn_watcher "${project_dir%/}"
done

echo ""
echo "=== Lint watcher: ${#project_dirs[@]} projects. Waiting... ==="

# Monitor loop: restart dead watchers + detect new projects
while true; do
    # Restart dead watchers
    i=0
    while [[ $i -lt ${#project_dirs[@]} ]]; do
        pid="${child_pids[$i]}"
        if ! kill -0 "$pid" 2>/dev/null; then
            project_dir="${project_dirs[$i]}"
            name=$(basename "$project_dir")
            if [[ -f "$project_dir/Cargo.toml" ]]; then
                echo "Restarting watcher for $name (PID $pid died)"
                # Remove old entry, spawn fresh
                project_dirs=("${project_dirs[@]:0:$i}" "${project_dirs[@]:$((i+1))}")
                child_pids=("${child_pids[@]:0:$i}" "${child_pids[@]:$((i+1))}")
                spawn_watcher "$project_dir"
                continue  # don't increment i — array shifted
            else
                echo "Removing watcher for $name (project gone)"
                project_dirs=("${project_dirs[@]:0:$i}" "${project_dirs[@]:$((i+1))}")
                child_pids=("${child_pids[@]:0:$i}" "${child_pids[@]:$((i+1))}")
                continue
            fi
        fi
        i=$((i + 1))
    done

    # Detect new projects — run lint immediately to catch current state
    for project_dir in "$RUST_DIR"/*/; do
        name=$(basename "$project_dir")
        project_dir="${project_dir%/}"
        [[ ! -f "$project_dir/Cargo.toml" ]] && continue
        is_excluded "$name" && continue
        is_watched "$project_dir" && continue
        echo "New project detected: $name"
        spawn_watcher "$project_dir"
    done

    sleep 1
done
