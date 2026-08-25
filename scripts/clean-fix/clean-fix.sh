#!/bin/bash
# Clean-fix style orchestrator.
# Usage: clean-fix.sh [project]
#        clean-fix.sh run_once
#   [project] — optionally filter the style eval, review, and fix pass
#   run_once — run one pass across all configured projects, ignoring persistent
#              stage enablement

set -euo pipefail

RUN_ONCE_REQUESTED="false"
PROJECT_FILTER=""
if [[ $# -gt 0 ]]; then
    case "$1" in
        run_once)
            RUN_ONCE_REQUESTED="true"
            if [[ $# -gt 1 ]]; then
                echo "Usage: clean-fix.sh run_once" >&2
                exit 1
            fi
            export CLEAN_FIX_FORCE_STYLE_STAGES=1
            ;;
        *)
            PROJECT_FILTER="$1"
            if [[ $# -gt 1 ]]; then
                echo "Usage: clean-fix.sh [project]" >&2
                exit 1
            fi
            ;;
    esac
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$HOME/.local/logs/clean-fix"
LOG_FILE="$LOG_DIR/clean-fix-$(date '+%Y%m%d-%H%M%S').log"
LEGACY_LOG="$HOME/.local/logs/clean-fix.log"
CONF_FILE="$SCRIPT_DIR/clean-fix.conf"
RUN_LOG_RETENTION_MINUTES=1440
MANUAL_LOG_RETENTION_DAYS=7

source "$HOME/.cargo/env"
source "$SCRIPT_DIR/agent_assignments.sh"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

mkdir -p "$LOG_DIR"
# The pipeline runs every 10 minutes around the clock. Keep roughly one
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

# Parse the active-checkout redirects and reject stale style settings.
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
    log "=== Starting clean-fix (project: $PROJECT_FILTER) ==="
elif [[ "$RUN_ONCE_REQUESTED" == "true" ]]; then
    log "=== Starting clean-fix (run_once) ==="
else
    log "=== Starting clean-fix ==="
fi
if [[ "$RUN_ONCE_REQUESTED" == "true" ]]; then
    log_run_once_summary
fi

# Back-populate canonical settings.local.json permissions before every pass:
# the style-fix agents depend on these permissions and the script is cheap.
log "SETTINGS: back-populating canonical permissions..."
python3 "$SCRIPT_DIR/backpopulate_settings.py" --apply >> "$LOG_FILE" 2>&1 || {
    log "WARNING: settings back-population failed"
}

# Run style evaluations and fixes when their stage assignments are enabled.
style_args=()
if [[ -n "$PROJECT_FILTER" ]]; then
    style_args+=("$(project_filter_key "$PROJECT_FILTER")")
fi
if [[ "$STYLE_EVAL_ENABLED" == "true" || "$RUN_ONCE_REQUESTED" == "true" ]]; then
    log "Starting style evaluations with family=$STYLE_EVAL_AGENT agent=${STYLE_EVAL_MODEL:-<default>} effort=${STYLE_EVAL_EFFORT:-<default>}..."
    "$SCRIPT_DIR/style-eval-all.sh" ${style_args[@]+"${style_args[@]}"} 2>&1 | tee -a "$LOG_FILE" || {
        log "WARNING: style evaluation script failed"
    }
else
    log "SKIP: style eval disabled in agent-assignments.conf"
fi

# Review pass over each project's pending evaluation markdown before the
# fix stage spawns.
if [[ "$STYLE_REVIEW_ENABLED" == "true" || "$RUN_ONCE_REQUESTED" == "true" ]]; then
    log "Reviewing pending evaluation markdown with family=$STYLE_REVIEW_AGENT agent=${STYLE_REVIEW_MODEL:-<default>} effort=${STYLE_REVIEW_EFFORT:-<default>}..."
    "$SCRIPT_DIR/style-eval-review-all.sh" ${style_args[@]+"${style_args[@]}"} 2>&1 | tee -a "$LOG_FILE" || {
        log "WARNING: style eval review script failed"
    }
else
    log "SKIP: style eval review disabled in agent-assignments.conf"
fi

if [[ "$STYLE_FIX_ENABLED" == "true" || "$RUN_ONCE_REQUESTED" == "true" ]]; then
    log "Creating style-fix worktrees with family=$STYLE_FIX_AGENT agent=${STYLE_FIX_MODEL:-<default>} effort=${STYLE_FIX_EFFORT:-<default>}..."
    "$SCRIPT_DIR/style-fix-worktrees.sh" ${style_args[@]+"${style_args[@]}"} 2>&1 | tee -a "$LOG_FILE" || {
        log "WARNING: style-fix worktree script failed"
    }
else
    log "SKIP: style fix disabled in agent-assignments.conf"
fi

ELAPSED=$(( SECONDS - START_TIME ))
MINUTES=$(( ELAPSED / 60 ))
SECS=$(( ELAPSED % 60 ))
log "=== Clean-fix complete (${MINUTES}m ${SECS}s) ==="

# Generate the clean-fix report via the assigned agent — but only when the run did
# something. The pipeline fires every 10 minutes; an all-SKIP cycle has no
# OK/FAILED lines and an agent call per idle cycle is pure cost.
REPORT_FILE="/tmp/clean-fix-report.txt"
REPORT_PROMPT_FILE="${LOG_FILE%.log}-report-prompt.md"
REPORT_LOG_FILE="$LOG_DIR/report_render.txt"
if grep -qE '(^|[[:space:]])(OK|FAILED|ERROR|TIMEOUT|RECOVERED|Launched):' "$LOG_FILE"; then
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
