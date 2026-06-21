#!/bin/bash
# Clean-fix orchestrator.
# Usage: clean-fix.sh [clean|style|all]   (default: all)
#   clean — settings back-populate + cargo clean/build/mend + warmup
#           (nightly via com.natemccoy.cargo-clean, 4:00 AM calendar)
#   style — style eval + review + fix worktrees
#           (every 10 min via com.natemccoy.style-fix, no idle gate)
#   all   — both, in order (manual /clean_fix run)
# The launchd triggers share one pgrep guard on this script's path, so the
# two scopes never run concurrently.

set -euo pipefail

SCOPE="${1:-all}"
case "$SCOPE" in
    clean|style|all) ;;
    *) echo "Usage: clean-fix.sh [clean|style|all]"; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_DIR="$HOME/rust"
LOG_DIR="$HOME/.local/logs/clean-fix"
LOG_FILE="$LOG_DIR/clean-fix-$(date '+%Y%m%d-%H%M%S').log"
LEGACY_LOG="$HOME/.local/logs/clean-fix.log"
TIMESTAMP_DIR="$HOME/.local/state/clean-fix"
CONF_FILE="$SCRIPT_DIR/clean-fix.conf"

source "$HOME/.cargo/env"
source "$SCRIPT_DIR/agent_assignments.sh"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
export SCCACHE_CACHE_SIZE="30G"

mkdir -p "$LOG_DIR"
mkdir -p "$TIMESTAMP_DIR"
# The style scope runs every 10 minutes around the clock — prune run logs
# older than 3 days so the log dir doesn't accumulate hundreds of files.
find "$LOG_DIR" -name 'clean-fix-*.log' -mtime +3 -delete 2>/dev/null || true
> "$LOG_FILE"
# Maintain legacy single-file path as a symlink to the latest run so existing
# tooling and the launchd plist stdout sink keep working.
ln -sfn "$LOG_FILE" "$LEGACY_LOG"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOG_FILE"
}

# Per-project build environment from [project_env] in clean-fix.conf. Echoes the
# space-separated KEY=VALUE assignments for the given project, or nothing.
# cargo-mend needs RUSTC_BOOTSTRAP=1 to compile its rustc_private features on
# stable (the `imend` trick) — the global toolchain is stable.
project_env_for() {
    local proj="$1" line section=""
    [[ -f "$CONF_FILE" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"; line="${line## }"; line="${line%% }"
        [[ -z "$line" ]] && continue
        if [[ "$line" =~ ^\[(.+)\]$ ]]; then section="${BASH_REMATCH[1]}"; continue; fi
        if [[ "$section" == "project_env" && "$line" == "$proj="* ]]; then
            echo "${line#*=}"; return 0
        fi
    done < "$CONF_FILE"
}

# Parse conf file. [build] is an opt-in allowlist of directories to clean/build.
BUILD_TARGETS=()
STYLE_EVAL_ENABLED=""
STYLE_EVAL_AGENT=""
STYLE_EVAL_MODEL=""
STYLE_EVAL_EFFORT=""
STYLE_REVIEW_ENABLED=""
STYLE_REVIEW_AGENT=""
STYLE_REVIEW_MODEL=""
STYLE_REVIEW_EFFORT=""
STYLE_FIX_ENABLED=""
STYLE_FIX_AGENT=""
STYLE_FIX_MODEL=""
STYLE_FIX_EFFORT=""
if [[ -f "$CONF_FILE" ]]; then
    current_section=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="$(cf_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            current_section="${BASH_REMATCH[1]}"
            continue
        fi
        case "$current_section" in
            build)
                BUILD_TARGETS+=("$stripped")
                ;;
            style_eval)
                if [[ "$stripped" =~ ^mode= ]]; then
                    echo "ERROR: [style_eval] mode is no longer supported; agent settings moved to agent-assignments.conf" >&2
                    exit 1
                elif [[ "$stripped" =~ ^(enabled|agent|model|effort)= ]]; then
                    echo "ERROR: [style_eval] agent settings moved to $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE" >&2
                    exit 1
                fi
                ;;
            style_fix)
                if [[ "$stripped" =~ ^mode= ]]; then
                    echo "ERROR: [style_fix] mode is no longer supported; agent settings moved to agent-assignments.conf" >&2
                    exit 1
                elif [[ "$stripped" =~ ^(enabled|agent|model|effort)= ]]; then
                    echo "ERROR: [style_fix] agent settings moved to $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE" >&2
                    exit 1
                fi
                ;;
        esac
    done < "$CONF_FILE"
fi

cf_load_stage_assignment style_eval \
    STYLE_EVAL_ENABLED STYLE_EVAL_AGENT STYLE_EVAL_MODEL STYLE_EVAL_EFFORT || exit 1
cf_load_stage_assignment style_eval_review \
    STYLE_REVIEW_ENABLED STYLE_REVIEW_AGENT STYLE_REVIEW_MODEL STYLE_REVIEW_EFFORT || exit 1
cf_load_stage_assignment style_fix \
    STYLE_FIX_ENABLED STYLE_FIX_AGENT STYLE_FIX_MODEL STYLE_FIX_EFFORT || exit 1

START_TIME=$SECONDS
log "=== Starting clean-fix (scope: $SCOPE) ==="

# Back-populate canonical settings.local.json permissions. Runs in every scope:
# the style-fix agents depend on these permissions and the script is cheap.
log "SETTINGS: back-populating canonical permissions..."
python3 "$SCRIPT_DIR/backpopulate_settings.py" --apply >> "$LOG_FILE" 2>&1 || {
    log "WARNING: settings back-population failed"
}

if [[ "$SCOPE" != "style" ]]; then
# Guard so set -u doesn't trip on an empty allowlist expansion.
if [[ ${#BUILD_TARGETS[@]} -eq 0 ]]; then
    log "No [build] targets configured — skipping clean/build pass."
fi
for project_name in ${BUILD_TARGETS[@]+"${BUILD_TARGETS[@]}"}; do
    project_dir="$RUST_DIR/$project_name"

    # A listed target must be a Rust crate/workspace. A missing Cargo.toml means
    # the opt-in name is wrong, so surface it rather than skip silently. Worktree
    # checkouts (.git is a file) are valid build targets — each has its own target/.
    if [[ ! -f "$project_dir/Cargo.toml" ]]; then
        log "SKIP: $project_name (no Cargo.toml at $project_dir)"
        continue
    fi

    # Skip projects not modified since last run
    timestamp_file="$TIMESTAMP_DIR/$project_name"
    if [[ -f "$timestamp_file" ]]; then
        changed=$(find "$project_dir" \( -path "$project_dir/target" -o -path "$project_dir/.claude" \) -prune -o -newer "$timestamp_file" -type f -print -quit)
        if [[ -z "$changed" ]]; then
            log "SKIP: $project_name (not modified since last run)"
            continue
        fi
    fi

    # Per-project build env (e.g. cargo-mend needs RUSTC_BOOTSTRAP=1 on stable).
    proj_env=$(project_env_for "$project_name")
    [[ -n "$proj_env" ]] && log "ENV: $project_name ($proj_env)"

    log "CLEAN: $project_name"
    env $proj_env cargo clean --manifest-path "$project_dir/Cargo.toml" 2>> "$LOG_FILE" || {
        log "ERROR: cargo clean failed for $project_name"
        continue
    }

    log "BUILD: $project_name"
    env $proj_env cargo build --workspace --examples --manifest-path "$project_dir/Cargo.toml" 2>> "$LOG_FILE" || {
        log "ERROR: cargo build failed for $project_name"
        continue
    }

    log "MEND: $project_name"
    env $proj_env "$HOME/.claude/scripts/clippy/lint" mend --manifest-path "$project_dir/Cargo.toml" 2>> "$LOG_FILE" || {
        log "WARNING: cargo mend failed for $project_name"
    }

    touch "$timestamp_file"
    log "DONE: $project_name"
done

# Warm up specific projects by launching briefly then killing
"$SCRIPT_DIR/clean-fix-warmup.sh" 2>&1 | tee -a "$LOG_FILE" || {
    log "WARNING: warmup script failed"
}
fi  # SCOPE != style

# Run style evaluations and fixes when their stage assignments are enabled.
if [[ "$SCOPE" != "clean" ]]; then
    if [[ "$STYLE_EVAL_ENABLED" == "true" ]]; then
        log "Starting style evaluations with $STYLE_EVAL_AGENT..."
        "$SCRIPT_DIR/style-eval-all.sh" 2>&1 | tee -a "$LOG_FILE" || {
            log "WARNING: style evaluation script failed"
        }
    else
        log "SKIP: style eval disabled in agent-assignments.conf"
    fi

    # Review pass over each project's pending evaluation markdown before the
    # fix stage spawns.
    if [[ "$STYLE_REVIEW_ENABLED" == "true" ]]; then
        log "Reviewing pending evaluation markdown with $STYLE_REVIEW_AGENT..."
        "$SCRIPT_DIR/style-eval-review-all.sh" 2>&1 | tee -a "$LOG_FILE" || {
            log "WARNING: style eval review script failed"
        }
    else
        log "SKIP: style eval review disabled in agent-assignments.conf"
    fi

    if [[ "$STYLE_FIX_ENABLED" == "true" ]]; then
        log "Creating style-fix worktrees with $STYLE_FIX_AGENT..."
        "$SCRIPT_DIR/style-fix-worktrees.sh" 2>&1 | tee -a "$LOG_FILE" || {
            log "WARNING: style-fix worktree script failed"
        }
    else
        log "SKIP: style fix disabled in agent-assignments.conf"
    fi
elif [[ "$SCOPE" == "clean" ]]; then
    log "SKIP: style scope not selected"
fi

ELAPSED=$(( SECONDS - START_TIME ))
MINUTES=$(( ELAPSED / 60 ))
SECS=$(( ELAPSED % 60 ))
log "=== Clean-fix Rust clean + rebuild complete (${MINUTES}m ${SECS}s) ==="

# Generate the clean-fix report via Claude CLI — but only when the run did
# something. The style scope fires every 10 minutes; an all-SKIP cycle has no
# OK/FAILED/CLEAN/BUILD lines and a headless claude call per idle cycle is
# pure cost.
REPORT_FILE="/tmp/clean-fix-report.txt"
if grep -qE '(^|[[:space:]])(OK|FAILED|ERROR|TIMEOUT|RECOVERED|Launched|CLEAN|BUILD|MEND):' "$LOG_FILE"; then
    log "Generating clean-fix report..."
    claude --print --dangerously-skip-permissions --settings '{"sandbox":{"enabled":false}}' -- "$(sed 's/\$ARGUMENTS/rebuild/g' "$HOME/.claude/scripts/clean-fix/report-render.md")" > "$REPORT_FILE" 2>> "$LOG_FILE" || {
        log "WARNING: failed to generate clean-fix report"
    }
else
    log "Report skipped (no per-project activity this run)."
fi
