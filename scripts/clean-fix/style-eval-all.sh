#!/bin/bash
# Run style evaluations on all opt-in targets in parallel.
# Targets come from the [targets] allowlist in clean-fix.conf.
# Can be run standalone or called from clean-fix.sh.
#
# Usage: style-eval-all.sh [project_name]
#   If project_name is given, only evaluate that single target.
#   If omitted, evaluate every [targets] entry.
#
# Each [targets] line is either:
#   <dir>            a whole directory (single crate, or --workspace)
#   <dir>/<subpath>  one workspace member crate inside <dir>
# <dir> may be a primary repo or a worktree checkout (e.g. *_bevy_update).

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_DIR="$HOME/rust"
HISTORY_DIR="$HOME/rust/nate_style/.history"
FAILURE_LOG_DIR="$HISTORY_DIR/.failures"
CONF_FILE="$SCRIPT_DIR/clean-fix.conf"
CMD_FILE="$HOME/.claude/commands/style_eval.md"
HISTORY_HELPER="$SCRIPT_DIR/style_history.py"
HEARTBEAT_HELPER="$SCRIPT_DIR/style-eval-heartbeat.sh"
LOG_DIR="/private/tmp/claude"
SINGLE_PROJECT="${1:-}"
STYLE_AGENT_MODE="claude"
CODEX_BIN="${CODEX_BIN:-$HOME/.nvm/versions/node/v20.19.1/bin/codex}"
HEARTBEAT_INTERVAL_SECS=60

mkdir -p "$LOG_DIR"
mkdir -p "$FAILURE_LOG_DIR"

# Parse conf file for the [targets] allowlist and style_eval settings.
targets=()    # opt-in eval targets: <dir> or <dir>/<subpath>
MAX_NEW_FINDINGS=""
STYLE_AGENT_MODEL=""
AGENT_TIMEOUT_SECS=""

if [[ ! -f "$CONF_FILE" ]]; then
    echo "ERROR: conf file not found: $CONF_FILE" >&2
    echo "       [style_eval] max_new_findings must be set there." >&2
    exit 1
fi

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
            targets) targets+=("$stripped") ;;
            style_eval)
                if [[ "$stripped" =~ ^mode=(.+)$ ]]; then
                    STYLE_AGENT_MODE="${BASH_REMATCH[1]}"
                elif [[ "$stripped" =~ ^model=(.+)$ ]]; then
                    STYLE_AGENT_MODEL="${BASH_REMATCH[1]}"
                elif [[ "$stripped" =~ ^enabled=(.+)$ ]]; then
                    if [[ "${BASH_REMATCH[1]}" == "true" ]]; then
                        STYLE_AGENT_MODE="claude"
                    else
                        STYLE_AGENT_MODE="off"
                    fi
                fi
                if [[ "$stripped" =~ ^max_new_findings=([0-9]+)$ ]]; then
                    MAX_NEW_FINDINGS="${BASH_REMATCH[1]}"
                fi
                ;;
            style_fix)
                if [[ "$stripped" =~ ^agent_timeout_secs=([0-9]+)$ ]]; then
                    AGENT_TIMEOUT_SECS="${BASH_REMATCH[1]}"
                fi
                ;;
        esac
    done < "$CONF_FILE"
fi

if [[ -z "$MAX_NEW_FINDINGS" ]]; then
    echo "ERROR: [style_eval] max_new_findings is not set in $CONF_FILE" >&2
    exit 1
fi

# Backstop timeout for a single eval agent. Reuses the [style_fix] agent cap
# (defaults to 2h if unset). Bounds a wedged agent so it cannot stall the serial
# wait loop forever — the failure that left a 12h-old run holding the launchd
# trigger's pgrep concurrency guard open, which suppressed every nightly run.
# The common trigger is the model issuing `rg PATTERN` with no path argument:
# run non-interactively, rg reads from stdin, and that stdin is a pipe that
# never closes, so rg blocks on read() indefinitely.
AGENT_TIMEOUT_SECS="${AGENT_TIMEOUT_SECS:-7200}"

# True if the agent's log shows at least one real invocation of
# style_history.py next-unit/record-unit. The prompt text quotes these
# commands too, so we filter to codex's exec marker — codex appends
# ` in <cwd>` after every shell command it actually runs. For claude,
# match the bash tool result preamble. Both narrow past prompt mentions.
agent_called_helper() {
    local log_file="$1"
    [[ -f "$log_file" ]] || return 1
    grep -Eq 'style_history\.py[^\n]*(next-unit|record-unit)[^\n]*in /Users/' "$log_file" && return 0
    grep -Eq '<bash-stdout>[^<]*style_history\.py[^<]*(next-unit|record-unit)' "$log_file"
}

# Persist a per-failure report so clean-fix issues don't require re-investigation
# from the raw codex/claude transcript. Written even when a retry recovers,
# so trends in agent reliability are visible over time.
write_failure_report() {
    local proj="$1"
    local a1_log="$2" a1_code="$3" a1_helper="$4"
    local a2_log="$5" a2_code="$6" a2_helper="$7"
    local outcome="$8"
    local ts
    ts=$(date -u +'%Y%m%dT%H%M%SZ')
    local report="$FAILURE_LOG_DIR/${ts}_${proj}.md"
    {
        echo "# Style eval failure: $proj"
        echo
        echo "- Timestamp (UTC): $ts"
        echo "- Agent mode: $STYLE_AGENT_MODE"
        echo "- Model: ${STYLE_AGENT_MODEL:-<default>}"
        echo "- Final outcome: $outcome"
        echo
        echo "## Attempt 1"
        echo "- log: $a1_log"
        echo "- exit code: $a1_code"
        echo "- helper invoked: $a1_helper"
        echo
        echo "### Last 30 lines"
        echo '```'
        tail -30 "$a1_log" 2>/dev/null || echo "(log missing)"
        echo '```'
        if [[ -n "$a2_log" ]]; then
            echo
            echo "## Attempt 2 (retry)"
            echo "- log: $a2_log"
            echo "- exit code: $a2_code"
            echo "- helper invoked: $a2_helper"
            echo
            echo "### Last 30 lines"
            echo '```'
            tail -30 "$a2_log" 2>/dev/null || echo "(log missing)"
            echo '```'
        fi
    } > "$report"
    echo "  failure report: $report"
}

# Signal a PID and every descendant in one ps snapshot. Used to tear down a
# wedged eval agent: the backgrounded subshell, its claude/codex child, and any
# rg/zsh grandchildren. Killing only the subshell orphans a stuck `rg` (blocked
# reading a never-closing stdin), so it keeps running — which is what left four
# orphaned rg pipelines alive for 11h in the incident this guards against.
kill_tree() {
    local root="$1" sig="${2:-TERM}"
    local pids
    pids=$(python3 - "$root" <<'PY'
import subprocess, sys
root = int(sys.argv[1])
out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
kids = {}
for line in out.split("\n"):
    f = line.split()
    if len(f) >= 2:
        kids.setdefault(int(f[1]), []).append(int(f[0]))
seen, stack = [], [root]
while stack:
    p = stack.pop()
    if p in seen:
        continue
    seen.append(p)
    stack += kids.get(p, [])
print(" ".join(str(p) for p in seen))
PY
) || return 0
    [[ -n "$pids" ]] && kill "-$sig" $pids 2>/dev/null || true
}

WRAPPER_HEARTBEAT_PID=""
NO_FINDINGS_CLEANUP_PID=""

start_wrapper_heartbeat() {
    local pid="$1" project="$2" project_root="$3" eval_path="$4" log_file="$5" launch_message="$6"
    local results_file="/tmp/style-eval-results-${project}.json"

    "$HEARTBEAT_HELPER" \
        --project "$project" \
        --record heartbeat \
        --pid "$pid" \
        --project-root "$project_root" \
        --log-file "$log_file" \
        --eval-path "$eval_path" \
        --results-file "$results_file" \
        --message "$launch_message" || true

    (
        local waited=0
        while kill -0 "$pid" 2>/dev/null; do
            sleep "$HEARTBEAT_INTERVAL_SECS"
            kill -0 "$pid" 2>/dev/null || exit 0
            waited=$((waited + HEARTBEAT_INTERVAL_SECS))
            "$HEARTBEAT_HELPER" \
                --project "$project" \
                --record heartbeat \
                --pid "$pid" \
                --project-root "$project_root" \
                --log-file "$log_file" \
                --eval-path "$eval_path" \
                --results-file "$results_file" \
                --message "wrapper running ${waited}s" || true
        done
    ) &
    WRAPPER_HEARTBEAT_PID=$!
}

no_findings_marker() {
    local project="$1"
    echo "$LOG_DIR/style_eval_${project}.no_findings_finalized"
}

has_real_style_fix_worktree() {
    local project="$1" project_root="$2"
    local worktree_dir="$RUST_DIR/${project}_style_fix"
    [[ -d "$worktree_dir" ]] || return 1
    git -C "$worktree_dir" rev-parse --git-dir >/dev/null 2>&1 || return 1
    git -C "$project_root" worktree list --porcelain 2>/dev/null \
        | grep -qF "worktree $worktree_dir"
}

clean_project_scratch() {
    local project="$1" project_root="$2"
    rm -f \
        "$LOG_DIR/style_eval_${project}_evaluation.md" \
        "$LOG_DIR/style_eval_review_${project}_evaluation.md" \
        "$(no_findings_marker "$project")"
    if ! has_real_style_fix_worktree "$project" "$project_root"; then
        rm -f "$LOG_DIR/style_fix_${project}_evaluation.md"
    fi
}

evaluation_status_field() {
    local project="$1" field="$2"
    python3 "$HISTORY_HELPER" evaluation-status --project "$project" --field "$field" 2>/dev/null || echo "missing"
}

evaluation_status_summary() {
    local project="$1"
    local lines findings coverage stop
    lines=$(evaluation_status_field "$project" line_count)
    findings=$(evaluation_status_field "$project" finding_count)
    coverage=$(evaluation_status_field "$project" coverage)
    stop=$(evaluation_status_field "$project" stop_reason)
    [[ -z "$coverage" || "$coverage" == "missing" ]] && coverage="unknown"
    [[ -z "$stop" || "$stop" == "missing" ]] && stop="in_progress"
    echo "${lines} lines; findings=${findings}; coverage=${coverage}; stop=${stop}"
}

pending_evaluation_has_no_findings() {
    local project="$1"
    [[ "$(evaluation_status_field "$project" status)" == "no_findings" ]]
}

pid_is_live_non_zombie() {
    local pid="$1"
    local stat
    kill -0 "$pid" 2>/dev/null || return 1
    stat=$(ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]')
    [[ -n "$stat" && "$stat" != Z* ]]
}

finalize_no_findings_if_ready() {
    local project="$1" eval_path="$2" marker="$3" project_root="${4:-}"
    pending_evaluation_has_no_findings "$project" || return 1
    local summary
    summary=$(evaluation_status_summary "$project")
    if python3 "$HISTORY_HELPER" finalize-no-findings --project "$project"; then
        if [[ -n "$project_root" ]]; then
            clean_project_scratch "$project" "$project_root"
        fi
        rm -f "$eval_path"
        : > "$marker"
        echo "AUTOFINALIZE: $project (no findings — $summary; recorded in history, pending finalized)"
        return 0
    fi
    echo "WARN: $project (could not auto-finalize no-findings history)"
    return 1
}

start_no_findings_cleanup() {
    local pid="$1" project="$2" eval_path="$3" project_root="$4"
    local marker
    marker=$(no_findings_marker "$project")
    rm -f "$marker"

    (
        while true; do
            if finalize_no_findings_if_ready "$project" "$eval_path" "$marker" "$project_root"; then
                if pid_is_live_non_zombie "$pid"; then
                    sleep 2
                    if pid_is_live_non_zombie "$pid"; then
                        echo "AUTOFINALIZE: $project (agent pid $pid still alive after final no-findings file — terminating)"
                        kill_tree "$pid" TERM
                    fi
                fi
                exit 0
            fi
            pid_is_live_non_zombie "$pid" || exit 0
            sleep 5
        done
    ) &
    NO_FINDINGS_CLEANUP_PID=$!
}

# Wait for an eval agent, killing its whole process tree if it overruns
# $timeout seconds. A background watchdog polls liveness and, on overrun, sends
# SIGTERM then SIGKILL to the tree; the foreground `wait` returns the moment the
# agent exits or is killed. Without this, a single hung agent blocks the serial
# wait loop indefinitely (see AGENT_TIMEOUT_SECS above).
wait_or_timeout() {
    local pid="$1" timeout="$2" project="$3" project_root="$4" eval_path="$5" log_file="$6"
    local results_file="/tmp/style-eval-results-${project}.json"
    (
        local waited=0
        while [[ $waited -lt $timeout ]]; do
            kill -0 "$pid" 2>/dev/null || exit 0
            sleep 5
            waited=$((waited + 5))
        done
        "$HEARTBEAT_HELPER" \
            --project "$project" \
            --record heartbeat \
            --pid "$pid" \
            --project-root "$project_root" \
            --log-file "$log_file" \
            --eval-path "$eval_path" \
            --results-file "$results_file" \
            --message "wrapper timeout after ${timeout}s" || true
        echo "TIMEOUT: eval agent (pid $pid) exceeded ${timeout}s — killing process tree"
        kill_tree "$pid" TERM
        sleep 5
        kill_tree "$pid" KILL
    ) &
    local watchdog=$!
    local code=0
    wait "$pid" || code=$?
    kill "$watchdog" 2>/dev/null || true
    wait "$watchdog" 2>/dev/null || true
    return $code
}

# Build the per-project prompt and invoke the configured style agent.
launch_eval() {
    local project_root="$1" worktree_eval="$2" eval_path="$3" log_file="$4"
    local prompt
    prompt="$(
        sed \
            -e "s|\$ARGUMENTS|$project_root|g" \
            -e "s|\$WORKTREE_EVAL_PATH|$worktree_eval|g" \
            -e "s|\$EVALUATION_PATH|$eval_path|g" \
            "$CMD_FILE"
    )"
    run_style_agent "$project_root" "$prompt" "$log_file"
}

run_style_agent() {
    local project_root="$1"
    local prompt="$2"
    local log_file="$3"
    local final_prompt="$prompt"

    case "$STYLE_AGENT_MODE" in
        claude)
            claude --print --dangerously-skip-permissions --settings '{"sandbox":{"enabled":false}}' -- "$final_prompt" > "$log_file" 2>&1
            ;;
        codex)
            final_prompt=$'IMPORTANT: Do NOT spawn sub-agents, delegate, or parallelize through helper agents. Complete this evaluation yourself in a single agent run.\nIMPORTANT: Do NOT create, replace, repair, or symlink the workspace path or any parent/peer repo path. If the expected workspace path is missing or invalid, fail and report it instead of trying to reconstruct it.\n\n'"$prompt"
            local codex_args=()
            if [[ -n "$STYLE_AGENT_MODEL" ]]; then
                codex_args+=("-m" "$STYLE_AGENT_MODEL")
            fi
            "$CODEX_BIN" exec \
                "${codex_args[@]}" \
                -c model_reasoning_effort='"high"' \
                --ephemeral \
                --full-auto \
                -C "$project_root" \
                --add-dir "$HISTORY_DIR" \
                -- "$final_prompt" \
                > "$log_file" 2>&1
            ;;
        *)
            echo "unsupported style_eval mode: $STYLE_AGENT_MODE" > "$log_file"
            return 1
            ;;
    esac
}

if [[ "$STYLE_AGENT_MODE" == "off" ]]; then
    echo "Style evaluation mode is off."
    exit 0
fi

if [[ "$STYLE_AGENT_MODE" == "codex" && ! -x "$CODEX_BIN" ]]; then
    echo "ERROR: configured Codex binary is not executable: $CODEX_BIN" >&2
    exit 1
fi

# Build project list. Parallel arrays indexed by position:
#   projects[i]             -- project name
#   project_roots[i]        -- absolute path to project root (for $ARGUMENTS)
#   project_worktree_evals[i] -- scratch evaluation path used by the style-fix worktree
projects=()
project_roots=()
project_worktree_evals=()

# Resolve each [targets] entry into (name, project_root, scratch eval path).
#   <dir>            -> whole directory; name=<dir>
#   <dir>/<subpath>  -> workspace member; name=last path segment
# <dir> may be a primary repo or a worktree checkout — the eval reads source
# and never touches git, so the two are indistinguishable here.
for entry in ${targets[@]+"${targets[@]}"}; do
    if [[ "$entry" == */* ]]; then
        name="${entry##*/}"
        subpath="${entry#*/}"
        project_root="${RUST_DIR}/${entry}"
        worktree_eval="${LOG_DIR}/style_fix_${name}_evaluation.md"
    else
        name="$entry"
        project_root="${RUST_DIR}/${entry}"
        worktree_eval="${LOG_DIR}/style_fix_${name}_evaluation.md"
    fi

    if [[ -n "$SINGLE_PROJECT" && "$name" != "$SINGLE_PROJECT" ]]; then
        continue
    fi
    if [[ ! -d "$project_root" ]]; then
        echo "SKIP: $name (target path not found: $project_root)"
        continue
    fi
    if [[ ! -f "$project_root/Cargo.toml" ]]; then
        echo "SKIP: $name (no Cargo.toml at $project_root)"
        continue
    fi

    projects+=("$name")
    project_roots+=("$project_root")
    project_worktree_evals+=("$worktree_eval")
done

if [[ ${#projects[@]} -eq 0 ]]; then
    echo "No projects to evaluate."
    exit 0
fi

echo "=== Style evaluation: ${#projects[@]} projects ==="

# Launch all evaluations in parallel.
# Use parallel arrays for the wait loop.
pids=()
names=()
roots_for_wait=()
worktrees_for_wait=()
evals_for_wait=()
heartbeats_for_wait=()
cleanups_for_wait=()
for i in "${!projects[@]}"; do
    proj="${projects[$i]}"
    project_root="${project_roots[$i]}"
    worktree_eval="${project_worktree_evals[$i]}"
    eval_path="$LOG_DIR/style_eval_${proj}_evaluation.md"

    pending_status=$(evaluation_status_field "$proj" status)
    if [[ "$pending_status" == "findings" || "$pending_status" == "reviewed_findings" ]]; then
        echo "SKIP: $proj (pending findings)"
        continue
    fi
    if [[ "$pending_status" == "no_findings" ]]; then
        summary=$(evaluation_status_summary "$proj")
        if python3 "$HISTORY_HELPER" finalize-no-findings --project "$proj"; then
            clean_project_scratch "$proj" "$project_root"
            echo "OK: $proj (no findings — $summary; recorded in history, pending finalized)"
        else
            echo "FAILED: $proj (could not finalize no-findings history)"
        fi
        continue
    fi

    existing_findings=0
    if [[ "$existing_findings" -ge "$MAX_NEW_FINDINGS" ]]; then
        echo "SKIP: $proj (already at cap of $MAX_NEW_FINDINGS findings)"
        continue
    fi

    python3 "$HISTORY_HELPER" start-run \
        --project-root "$project_root" || {
        echo "FAILED: $proj (could not start pending run)"
        continue
    }
    clean_project_scratch "$proj" "$project_root"

    log_file="$LOG_DIR/style_eval_${proj}.log"
    launch_eval "$project_root" "$worktree_eval" "$eval_path" "$log_file" &
    agent_pid=$!
    start_wrapper_heartbeat "$agent_pid" "$proj" "$project_root" "$eval_path" "$log_file" "wrapper launched"
    start_no_findings_cleanup "$agent_pid" "$proj" "$eval_path" "$project_root"
    pids+=("$agent_pid")
    names+=("$proj")
    roots_for_wait+=("$project_root")
    worktrees_for_wait+=("$worktree_eval")
    evals_for_wait+=("$eval_path")
    heartbeats_for_wait+=("$WRAPPER_HEARTBEAT_PID")
    cleanups_for_wait+=("$NO_FINDINGS_CLEANUP_PID")
    echo "Launched: $proj via $STYLE_AGENT_MODE (PID $agent_pid)"
done

echo ""
echo "Waiting for ${#pids[@]} processes..."

# Wait for all and track results. On a missing pending evaluation, retry once
# serially before recording the failure. Every failure (recovered or not)
# writes a persistent report under $FAILURE_LOG_DIR so the next-morning
# triage doesn't require re-reading a 350 KB codex transcript.
failed=0
succeeded=0
recovered=0
idx=0
# Bash 3.2 trips set -u on empty arrays expanded as "${pids[@]}", so guard.
if [[ ${#pids[@]} -gt 0 ]]; then
for pid in "${pids[@]}"; do
    name="${names[$idx]}"
    project_root="${roots_for_wait[$idx]}"
    worktree_eval="${worktrees_for_wait[$idx]}"
    eval_path="${evals_for_wait[$idx]}"
    heartbeat_pid="${heartbeats_for_wait[$idx]}"
    cleanup_pid="${cleanups_for_wait[$idx]}"
    log_file="$LOG_DIR/style_eval_${name}.log"
    no_findings_done_marker=$(no_findings_marker "$name")
    wait_or_timeout "$pid" "$AGENT_TIMEOUT_SECS" "$name" "$project_root" "$eval_path" "$log_file" && code=0 || code=$?
    kill "$heartbeat_pid" 2>/dev/null || true
    wait "$heartbeat_pid" 2>/dev/null || true
    kill "$cleanup_pid" 2>/dev/null || true
    wait "$cleanup_pid" 2>/dev/null || true

    if [[ -f "$no_findings_done_marker" ]]; then
        echo "OK: $name (no findings — already recorded in history, pending finalized)"
        succeeded=$((succeeded + 1))
        idx=$((idx + 1))
        continue
    fi

    eval_status=$(evaluation_status_field "$name" status)
    if [[ "$eval_status" == "missing" ]]; then
        a1_helper="no"
        if agent_called_helper "$log_file"; then a1_helper="yes"; fi
        echo "FAILED attempt 1: $name (exit $code, helper-invoked=$a1_helper) — retrying once"

        # Preserve attempt-1 log next to attempt-2 so both end up in the report.
        a1_log="${log_file%.log}.attempt1.log"
        mv -f "$log_file" "$a1_log" 2>/dev/null || a1_log="$log_file"

        # Clean pending state and start a fresh run before the retry. If start-run
        # itself fails we still write a report so the cause is captured.
        python3 "$HISTORY_HELPER" discard-pending --project "$name" || true
        if ! python3 "$HISTORY_HELPER" start-run --project-root "$project_root"; then
            write_failure_report "$name" "$a1_log" "$code" "$a1_helper" \
                "" "" "" "failed-no-retry (start-run rejected)"
            echo "FAILED: $name (could not start retry — start-run rejected)"
            failed=$((failed + 1))
            idx=$((idx + 1))
            continue
        fi
        clean_project_scratch "$name" "$project_root"

        launch_eval "$project_root" "$worktree_eval" "$eval_path" "$log_file" &
        retry_pid=$!
        start_wrapper_heartbeat "$retry_pid" "$name" "$project_root" "$eval_path" "$log_file" "wrapper retry launched"
        retry_heartbeat_pid="$WRAPPER_HEARTBEAT_PID"
        start_no_findings_cleanup "$retry_pid" "$name" "$eval_path" "$project_root"
        retry_cleanup_pid="$NO_FINDINGS_CLEANUP_PID"
        no_findings_done_marker=$(no_findings_marker "$name")
        wait_or_timeout "$retry_pid" "$AGENT_TIMEOUT_SECS" "$name" "$project_root" "$eval_path" "$log_file" && retry_code=0 || retry_code=$?
        kill "$retry_heartbeat_pid" 2>/dev/null || true
        wait "$retry_heartbeat_pid" 2>/dev/null || true
        kill "$retry_cleanup_pid" 2>/dev/null || true
        wait "$retry_cleanup_pid" 2>/dev/null || true
        a2_helper="no"
        if agent_called_helper "$log_file"; then a2_helper="yes"; fi

        if [[ -f "$no_findings_done_marker" ]]; then
            echo "RECOVERED: $name (no findings — already recorded in history, pending finalized, retry succeeded)"
            recovered=$((recovered + 1))
            succeeded=$((succeeded + 1))
        else
            retry_status=$(evaluation_status_field "$name" status)
            if [[ "$retry_status" == "no_findings" ]]; then
                summary=$(evaluation_status_summary "$name")
                python3 "$HISTORY_HELPER" finalize-no-findings --project "$name" || true
                clean_project_scratch "$name" "$project_root"
                echo "RECOVERED: $name (no findings — $summary; recorded in history, pending finalized, retry succeeded)"
                recovered=$((recovered + 1))
                succeeded=$((succeeded + 1))
            elif [[ "$retry_status" == "findings" || "$retry_status" == "reviewed_findings" ]]; then
                summary=$(evaluation_status_summary "$name")
                rm -f "$eval_path"
                echo "RECOVERED: $name ($summary, retry succeeded)"
                recovered=$((recovered + 1))
                succeeded=$((succeeded + 1))
            else
                python3 "$HISTORY_HELPER" discard-pending --project "$name" || true
                echo "FAILED: $name (retry also failed, exit=$retry_code helper-invoked=$a2_helper)"
                write_failure_report "$name" "$a1_log" "$code" "$a1_helper" \
                    "$log_file" "$retry_code" "$a2_helper" "failed-after-retry"
                failed=$((failed + 1))
            fi
        fi
        idx=$((idx + 1))
        continue
    fi

    if [[ $code -ne 0 ]]; then
        echo "WARN: $name (exit $code, but pending evaluation was produced)"
    fi
    if [[ "$eval_status" == "no_findings" ]]; then
        summary=$(evaluation_status_summary "$name")
        python3 "$HISTORY_HELPER" finalize-no-findings --project "$name" || {
            echo "FAILED: $name (could not finalize no-findings history)"
            failed=$((failed + 1))
            idx=$((idx + 1))
            continue
        }
        clean_project_scratch "$name" "$project_root"
        echo "OK: $name (no findings — $summary; recorded in history, pending finalized)"
    else
        summary=$(evaluation_status_summary "$name")
        rm -f "$eval_path"
        echo "OK: $name ($summary)"
    fi
    succeeded=$((succeeded + 1))
    idx=$((idx + 1))
done
fi

echo ""
if [[ $recovered -gt 0 ]]; then
    echo "=== Done: $succeeded succeeded ($recovered after retry), $failed failed out of ${#projects[@]} ==="
else
    echo "=== Done: $succeeded succeeded, $failed failed out of ${#projects[@]} ==="
fi
if [[ $failed -gt 0 || $recovered -gt 0 ]]; then
    echo "Failure reports: $FAILURE_LOG_DIR"
fi
