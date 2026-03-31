#!/bin/bash
# Nightly Rust clean + rebuild script
# Cleans target directories and rebuilds to prevent incremental bloat
# Runs via launchd at 4:00 AM

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_DIR="$HOME/rust"
LOG_FILE="$HOME/.local/logs/nightly-rust-clean-build.log"
TIMESTAMP_DIR="$HOME/.local/state/nightly-rust"
CONF_FILE="$SCRIPT_DIR/nightly-rust.conf"

source "$HOME/.cargo/env"
export PATH="$HOME/.local/bin:$PATH"
export SCCACHE_CACHE_SIZE="30G"

mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$TIMESTAMP_DIR"
> "$LOG_FILE"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOG_FILE"
}

# Parse conf file
EXCLUDE=()
STYLE_EVAL_ENABLED=true
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
        case "$current_section" in
            exclude)
                EXCLUDE+=("$stripped")
                ;;
            style_eval)
                if [[ "$stripped" =~ ^enabled=(.+)$ ]]; then
                    STYLE_EVAL_ENABLED="${BASH_REMATCH[1]}"
                fi
                ;;
        esac
    done < "$CONF_FILE"
fi

START_TIME=$SECONDS
log "=== Starting nightly Rust clean + rebuild ==="

for project_dir in "$RUST_DIR"/*/; do
    project_name=$(basename "$project_dir")

    # Skip excluded projects
    skip=false
    for exclude in "${EXCLUDE[@]}"; do
        if [[ "$project_name" == "$exclude" ]]; then
            skip=true
            break
        fi
    done
    if $skip; then
        log "SKIP: $project_name (excluded)"
        continue
    fi

    # Skip automation worktrees
    if [[ "$project_name" == *_style_fix ]]; then
        log "SKIP: $project_name (style-fix worktree)"
        continue
    fi

    # Skip worktree checkouts (only process primary repos)
    if [[ -f "$project_dir/.git" ]]; then
        log "SKIP: $project_name (worktree, not primary checkout)"
        continue
    fi

    # Skip non-Rust projects
    if [[ ! -f "$project_dir/Cargo.toml" ]]; then
        log "SKIP: $project_name (no Cargo.toml)"
        continue
    fi

    # Skip projects not modified since last run
    timestamp_file="$TIMESTAMP_DIR/$project_name"
    if [[ -f "$timestamp_file" ]]; then
        changed=$(find "$project_dir" -path "$project_dir/target" -prune -o -newer "$timestamp_file" -type f -print -quit)
        if [[ -z "$changed" ]]; then
            log "SKIP: $project_name (not modified since last run)"
            continue
        fi
    fi

    log "CLEAN: $project_name"
    cargo clean --manifest-path "$project_dir/Cargo.toml" 2>> "$LOG_FILE" || {
        log "ERROR: cargo clean failed for $project_name"
        continue
    }

    log "BUILD: $project_name"
    cargo build --workspace --examples --manifest-path "$project_dir/Cargo.toml" 2>> "$LOG_FILE" || {
        log "ERROR: cargo build failed for $project_name"
        continue
    }

    log "MEND: $project_name"
    cargo mend --manifest-path "$project_dir/Cargo.toml" 2>> "$LOG_FILE" || {
        log "WARNING: cargo mend failed for $project_name"
    }

    log "CLIPPY: $project_name"
    cargo clippy --workspace --all-targets --all-features --manifest-path "$project_dir/Cargo.toml" -- -D warnings 2>> "$LOG_FILE" || {
        log "WARNING: clippy failed for $project_name"
    }

    touch "$timestamp_file"
    log "DONE: $project_name"
done

# Warm up specific projects by launching briefly then killing
"$SCRIPT_DIR/nightly-warmup.sh" 2>&1 | tee -a "$LOG_FILE" || {
    log "WARNING: warmup script failed"
}

# Run style evaluations and fixes (if enabled)
if [[ "$STYLE_EVAL_ENABLED" == "true" ]]; then
    log "Starting style evaluations..."
    "$SCRIPT_DIR/style-eval-all.sh" 2>&1 | tee -a "$LOG_FILE" || {
        log "WARNING: style evaluation script failed"
    }

    log "Creating style-fix worktrees..."
    "$SCRIPT_DIR/style-fix-worktrees.sh" 2>&1 | tee -a "$LOG_FILE" || {
        log "WARNING: style-fix worktree script failed"
    }
else
    log "SKIP: style evaluations disabled in nightly-rust.conf"
fi

ELAPSED=$(( SECONDS - START_TIME ))
MINUTES=$(( ELAPSED / 60 ))
SECS=$(( ELAPSED % 60 ))
log "=== Nightly Rust clean + rebuild complete (${MINUTES}m ${SECS}s) ==="

# Generate the nightly report via Claude CLI
REPORT_FILE="/tmp/nightly-rust-report.txt"
log "Generating nightly report..."
claude --print --dangerously-skip-permissions --settings '{"sandbox":{"enabled":false}}' -- "$(sed 's/\$ARGUMENTS/rebuild/g' "$HOME/.claude/commands/nightly_report.md")" > "$REPORT_FILE" 2>> "$LOG_FILE" || {
    log "WARNING: failed to generate nightly report"
}
