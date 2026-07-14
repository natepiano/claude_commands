#!/bin/bash
# Clean-fix orchestrator.
# Usage: clean-fix.sh [clean|style] [project]
#        clean-fix.sh [project]
#        clean-fix.sh run_once
#   clean — settings back-populate + cargo clean/build/mend + warmup
#           (nightly via com.natemccoy.cargo-clean, 4:00 AM calendar)
#   style — style eval + review + fix worktrees
#           (every 10 min via com.natemccoy.style-fix, no idle gate)
#   run_once — one style eval + review + fix pass across all configured
#              projects, ignoring persistent stage enablement
#   no scope — both, in order (manual /clean_fix run)
# The launchd triggers share one pgrep guard on this script's path, so the
# two scopes never run concurrently.

set -euo pipefail

SCOPE="all"
PROJECT_FILTER=""
if [[ $# -gt 0 ]]; then
    case "$1" in
        clean|style|all)
            SCOPE="$1"
            PROJECT_FILTER="${2:-}"
            if [[ $# -gt 2 ]]; then
                echo "Usage: clean-fix.sh [clean|style] [project]" >&2
                exit 1
            fi
            ;;
        run_once)
            SCOPE="$1"
            if [[ $# -gt 1 ]]; then
                echo "Usage: clean-fix.sh run_once" >&2
                exit 1
            fi
            export CLEAN_FIX_FORCE_STYLE_STAGES=1
            ;;
        *)
            SCOPE="all"
            PROJECT_FILTER="$1"
            if [[ $# -gt 1 ]]; then
                echo "Usage: clean-fix.sh [clean|style] [project]" >&2
                exit 1
            fi
            ;;
    esac
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_DIR="$HOME/rust"
LOG_DIR="$HOME/.local/logs/clean-fix"
LOG_FILE="$LOG_DIR/clean-fix-$(date '+%Y%m%d-%H%M%S').log"
LEGACY_LOG="$HOME/.local/logs/clean-fix.log"
TIMESTAMP_DIR="$HOME/.local/state/clean-fix"
CONF_FILE="$SCRIPT_DIR/clean-fix.conf"
RUN_LOG_RETENTION_MINUTES=1440
MANUAL_LOG_RETENTION_DAYS=7

source "$HOME/.cargo/env"
source "$SCRIPT_DIR/agent_assignments.sh"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
export SCCACHE_CACHE_SIZE="30G"

mkdir -p "$LOG_DIR"
mkdir -p "$TIMESTAMP_DIR"
# The style scope runs every 10 minutes around the clock. Keep roughly one
# day of scheduled logs plus a short manual-log window so report lists stay
# focused on runs that are still useful to inspect.
find "$LOG_DIR" -name 'clean-fix-*.log' -mmin +"$RUN_LOG_RETENTION_MINUTES" -delete 2>/dev/null || true
find "$LOG_DIR" -name 'style-fix-manual-*.log' -mtime +"$MANUAL_LOG_RETENTION_DAYS" -delete 2>/dev/null || true
> "$LOG_FILE"
# Maintain legacy single-file path as a symlink to the latest run so existing
# tooling and the launchd plist stdout sink keep working.
ln -sfn "$LOG_FILE" "$LEGACY_LOG"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOG_FILE"
}

log_run_once_summary() {
    local eval_agent="${STYLE_EVAL_MODEL:-<default>}:${STYLE_EVAL_EFFORT:-<default>}"
    local review_agent="${STYLE_REVIEW_MODEL:-<default>}:${STYLE_REVIEW_EFFORT:-<default>}"
    local fix_agent="${STYLE_FIX_MODEL:-<default>}:${STYLE_FIX_EFFORT:-<default>}"

    log "Run-once execution summary: one eval -> eval_review -> fix pass across all configured style projects; persistent stage enablement ignored."
    {
        printf '%-12s %s\n' "Stage" "Agent:effort"
        printf '%-12s %s\n' "------------" "------------"
        printf '%-12s %s\n' "eval" "$eval_agent"
        printf '%-12s %s\n' "eval_review" "$review_agent"
        printf '%-12s %s\n' "fix" "$fix_agent"
    } | tee -a "$LOG_FILE"
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

project_key() {
    local entry="$1"
    if [[ "$entry" == */* ]]; then
        printf '%s' "${entry##*/}"
    else
        printf '%s' "$entry"
    fi
}

checkout_root() {
    local checkout="$1"
    printf '%s' "${checkout%%/*}"
}

active_redirect_index_for_build_target() {
    local target="$1"
    local i checkout root
    for ((i = 0; i < ${#cf_ac_keys[@]}; i++)); do
        checkout="${cf_ac_vals[$i]}"
        root="$(checkout_root "$checkout")"
        if [[ "$target" == "${cf_ac_keys[$i]}" || "$target" == "$checkout" || "$target" == "$root" || "$target" == "$root/"* ]]; then
            printf '%s' "$i"
            return
        fi
    done
    printf '%s' "-1"
}

project_identity_for_build_target() {
    local target="$1"
    local index
    index="$(active_redirect_index_for_build_target "$target")"
    if [[ "$index" == "-1" ]]; then
        project_key "$target"
    else
        project_key "${cf_ac_keys[$index]}"
    fi
}

project_display_for_build_target() {
    local target="$1"
    local index
    index="$(active_redirect_index_for_build_target "$target")"
    if [[ "$index" == "-1" ]]; then
        project_key "$target"
    else
        checkout_root "${cf_ac_vals[$index]}"
    fi
}

project_filter_key() {
    local filter="$1"
    local normalized i key checkout root
    normalized="$(project_key "$filter")"
    for ((i = 0; i < ${#cf_ac_keys[@]}; i++)); do
        key="$(project_key "${cf_ac_keys[$i]}")"
        checkout="${cf_ac_vals[$i]}"
        root="$(checkout_root "$checkout")"
        if [[ "$filter" == "${cf_ac_keys[$i]}" || "$filter" == "$checkout" || "$filter" == "$root" || "$filter" == "$root/"* || "$normalized" == "$key" ]]; then
            printf '%s' "$key"
            return
        fi
    done
    printf '%s' "$normalized"
}

build_target_matches_filter() {
    local target="$1"
    local filter="$2"
    local target_display target_identity filter_identity
    [[ -z "$filter" ]] && return 0
    target_display="$(project_display_for_build_target "$target")"
    target_identity="$(project_identity_for_build_target "$target")"
    filter_identity="$(project_filter_key "$filter")"
    [[ "$target" == "$filter" || "$target_display" == "$filter" || "$target_identity" == "$filter_identity" ]]
}

# Parse conf file. [build] is an opt-in allowlist of directories to clean/build.
BUILD_TARGETS=()
cf_ac_keys=()
cf_ac_vals=()
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
            active_checkout)
                if [[ "$stripped" == *=* ]]; then
                    key="$(cf_trim "${stripped%%=*}")"
                    value="$(cf_trim "${stripped#*=}")"
                    if [[ -n "$key" && -n "$value" ]]; then
                        cf_ac_keys+=("$key")
                        cf_ac_vals+=("$value")
                    fi
                fi
                ;;
            style_eval)
                if [[ "$stripped" =~ ^mode= ]]; then
                    echo "ERROR: [style_eval] stale clean-fix setting; stage enablement lives in $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE and agent settings live in $AGENTS_CONFIG_FILE" >&2
                    exit 1
                elif [[ "$stripped" =~ ^(enabled|agent|model|effort)= ]]; then
                    echo "ERROR: [style_eval] stale clean-fix setting; stage enablement lives in $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE and agent settings live in $AGENTS_CONFIG_FILE" >&2
                    exit 1
                fi
                ;;
            style_fix)
                if [[ "$stripped" =~ ^mode= ]]; then
                    echo "ERROR: [style_fix] stale clean-fix setting; stage enablement lives in $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE and agent settings live in $AGENTS_CONFIG_FILE" >&2
                    exit 1
                elif [[ "$stripped" =~ ^(enabled|agent|model|effort)= ]]; then
                    echo "ERROR: [style_fix] stale clean-fix setting; stage enablement lives in $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE and agent settings live in $AGENTS_CONFIG_FILE" >&2
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
if [[ -n "$PROJECT_FILTER" ]]; then
    log "=== Starting clean-fix (scope: $SCOPE, project: $PROJECT_FILTER) ==="
else
    log "=== Starting clean-fix (scope: $SCOPE) ==="
fi
if [[ "$SCOPE" == "run_once" ]]; then
    log_run_once_summary
fi

# Back-populate canonical settings.local.json permissions. Runs in every scope:
# the style-fix agents depend on these permissions and the script is cheap.
log "SETTINGS: back-populating canonical permissions..."
python3 "$SCRIPT_DIR/backpopulate_settings.py" --apply >> "$LOG_FILE" 2>&1 || {
    log "WARNING: settings back-population failed"
}

if [[ "$SCOPE" != "style" && "$SCOPE" != "run_once" ]]; then
# Guard so set -u doesn't trip on an empty allowlist expansion.
if [[ ${#BUILD_TARGETS[@]} -eq 0 ]]; then
    log "No [build] targets configured — skipping clean/build pass."
fi
matched_clean_target=false
for project_name in ${BUILD_TARGETS[@]+"${BUILD_TARGETS[@]}"}; do
    project_display="$(project_display_for_build_target "$project_name")"
    project_identity="$(project_identity_for_build_target "$project_name")"
    if ! build_target_matches_filter "$project_name" "$PROJECT_FILTER"; then
        continue
    fi
    matched_clean_target=true
    project_dir="$RUST_DIR/$project_name"

    # A listed target must be a Rust crate/workspace. A missing Cargo.toml means
    # the opt-in name is wrong, so surface it rather than skip silently. Worktree
    # checkouts (.git is a file) are valid build targets — each has its own target/.
    if [[ ! -f "$project_dir/Cargo.toml" ]]; then
        log "SKIP: $project_display (no Cargo.toml at $project_dir)"
        continue
    fi

    # Skip projects not modified since last run
    timestamp_file="$TIMESTAMP_DIR/$project_display"
    if [[ -f "$timestamp_file" ]]; then
        changed=$(find "$project_dir" \( -path "$project_dir/target" -o -path "$project_dir/.claude" \) -prune -o -newer "$timestamp_file" -type f -print -quit)
        if [[ -z "$changed" ]]; then
            log "SKIP: $project_display (not modified since last run)"
            continue
        fi
    fi

    # Per-project build env (e.g. cargo-mend needs RUSTC_BOOTSTRAP=1 on stable).
    proj_env=$(project_env_for "$project_name")
    if [[ -z "$proj_env" && "$project_display" != "$project_name" ]]; then
        proj_env=$(project_env_for "$project_display")
    fi
    if [[ -z "$proj_env" && "$project_identity" != "$project_display" ]]; then
        proj_env=$(project_env_for "$project_identity")
    fi
    [[ -n "$proj_env" ]] && log "ENV: $project_display ($proj_env)"

    log "CLEAN: $project_display"
    env $proj_env cargo clean --manifest-path "$project_dir/Cargo.toml" 2>> "$LOG_FILE" || {
        log "ERROR: cargo clean failed for $project_display"
        continue
    }

    log "BUILD: $project_display"
    env $proj_env cargo build --workspace --examples --manifest-path "$project_dir/Cargo.toml" 2>> "$LOG_FILE" || {
        log "ERROR: cargo build failed for $project_display"
        continue
    }

    log "MEND: $project_display"
    env $proj_env "$HOME/.claude/scripts/clippy/lint" mend --manifest-path "$project_dir/Cargo.toml" 2>> "$LOG_FILE" || {
        log "WARNING: cargo mend failed for $project_display"
    }

    touch "$timestamp_file"
    log "DONE: $project_display"
done

if [[ -n "$PROJECT_FILTER" && "$matched_clean_target" == "false" && "$SCOPE" == "clean" ]]; then
    log "SKIP: $PROJECT_FILTER (not listed in [build])"
fi

# Warm up specific projects by launching briefly then killing
"$SCRIPT_DIR/clean-fix-warmup.sh" ${PROJECT_FILTER:+"$PROJECT_FILTER"} 2>&1 | tee -a "$LOG_FILE" || {
    log "WARNING: warmup script failed"
}
fi  # SCOPE != style

# Run style evaluations and fixes when their stage assignments are enabled.
if [[ "$SCOPE" != "clean" ]]; then
    style_args=()
    if [[ -n "$PROJECT_FILTER" ]]; then
        style_args+=("$(project_filter_key "$PROJECT_FILTER")")
    fi
    if [[ "$STYLE_EVAL_ENABLED" == "true" || "$SCOPE" == "run_once" ]]; then
        log "Starting style evaluations with family=$STYLE_EVAL_AGENT agent=${STYLE_EVAL_MODEL:-<default>} effort=${STYLE_EVAL_EFFORT:-<default>}..."
        "$SCRIPT_DIR/style-eval-all.sh" ${style_args[@]+"${style_args[@]}"} 2>&1 | tee -a "$LOG_FILE" || {
            log "WARNING: style evaluation script failed"
        }
    else
        log "SKIP: style eval disabled in agent-assignments.conf"
    fi

    # Review pass over each project's pending evaluation markdown before the
    # fix stage spawns.
    if [[ "$STYLE_REVIEW_ENABLED" == "true" || "$SCOPE" == "run_once" ]]; then
        log "Reviewing pending evaluation markdown with family=$STYLE_REVIEW_AGENT agent=${STYLE_REVIEW_MODEL:-<default>} effort=${STYLE_REVIEW_EFFORT:-<default>}..."
        "$SCRIPT_DIR/style-eval-review-all.sh" ${style_args[@]+"${style_args[@]}"} 2>&1 | tee -a "$LOG_FILE" || {
            log "WARNING: style eval review script failed"
        }
    else
        log "SKIP: style eval review disabled in agent-assignments.conf"
    fi

    if [[ "$STYLE_FIX_ENABLED" == "true" || "$SCOPE" == "run_once" ]]; then
        log "Creating style-fix worktrees with family=$STYLE_FIX_AGENT agent=${STYLE_FIX_MODEL:-<default>} effort=${STYLE_FIX_EFFORT:-<default>}..."
        "$SCRIPT_DIR/style-fix-worktrees.sh" ${style_args[@]+"${style_args[@]}"} 2>&1 | tee -a "$LOG_FILE" || {
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

# Generate the clean-fix report via the assigned agent — but only when the run did
# something. The style scope fires every 10 minutes; an all-SKIP cycle has no
# OK/FAILED/CLEAN/BUILD lines and an agent call per idle cycle is pure cost.
REPORT_FILE="/tmp/clean-fix-report.txt"
REPORT_PROMPT_FILE="${LOG_FILE%.log}-report-prompt.md"
REPORT_LOG_FILE="$LOG_DIR/report_render.txt"
if grep -qE '(^|[[:space:]])(OK|FAILED|ERROR|TIMEOUT|RECOVERED|Launched|CLEAN|BUILD|MEND):' "$LOG_FILE"; then
    log "Generating clean-fix report..."
    if sed 's/\$ARGUMENTS/rebuild/g' "$HOME/.claude/scripts/clean-fix/report-render.md" > "$REPORT_PROMPT_FILE"; then
        "$HOME/.claude/scripts/agents/agent_exec.sh" cleanfix.report write \
            "$HOME/.claude" "$REPORT_PROMPT_FILE" "$REPORT_FILE" "$REPORT_LOG_FILE" || {
            log "WARNING: failed to generate clean-fix report"
        }
    else
        log "WARNING: failed to generate clean-fix report"
    fi
    rm -f "$REPORT_PROMPT_FILE"
else
    log "Report skipped (no per-project activity this run)."
fi
