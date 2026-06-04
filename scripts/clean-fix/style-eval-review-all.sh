#!/bin/bash
# Run the style-eval-review prompt on every project with fresh pending evaluation markdown.
# Runs after style-eval-all.sh and before style-fix-worktrees.sh in the
# clean-fix pipeline.
#
# The review agent follows the [style_eval] mode in clean-fix.conf: claude
# or codex (mode=off falls back to claude for standalone runs). The prompt lives
# at style-eval-review-prompt.md in this directory — it is not a slash command.
#
# Usage: style-eval-review-all.sh [project_name]
#   If project_name is given, only review that single project's pending evaluation.
#   If omitted, review every eligible pending evaluation.
#
# Eligibility:
#   - pending JSON has evaluation markdown
#   - the markdown has at least one `### N.` numbered finding
#   - the markdown does not yet contain a `## Review Log` section
#     (review is idempotent — already-reviewed evals are skipped)

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_DIR="$HOME/rust"
NATE_STYLE_DIR="$HOME/rust/nate_style"
CONF_FILE="$SCRIPT_DIR/clean-fix.conf"
CMD_FILE="$SCRIPT_DIR/style-eval-review-prompt.md"
HISTORY_HELPER="$SCRIPT_DIR/style_history.py"
LOG_DIR="/private/tmp/claude"
SINGLE_PROJECT="${1:-}"
STYLE_AGENT_MODE="claude"
STYLE_AGENT_MODEL=""
CODEX_BIN="${CODEX_BIN:-$HOME/.nvm/versions/node/v20.19.1/bin/codex}"

mkdir -p "$LOG_DIR"

# Parse conf for the agent mode/model and the [targets] allowlist, so the review
# project list matches the eval stage.
targets=()

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
                fi
                ;;
        esac
    done < "$CONF_FILE"
fi

# Review follows the configured eval agent. When eval is disabled (mode=off),
# fall back to claude so a standalone review run still works.
if [[ "$STYLE_AGENT_MODE" == "off" ]]; then
    STYLE_AGENT_MODE="claude"
fi
if [[ "$STYLE_AGENT_MODE" == "codex" && ! -x "$CODEX_BIN" ]]; then
    echo "ERROR: configured Codex binary is not executable: $CODEX_BIN" >&2
    exit 1
fi

# Substitute $ARGUMENTS into the prompt, then invoke the configured review agent.
# Mirrors run_style_agent in style-eval-all.sh; the review only edits
# evaluation markdown, so codex gets the workspace dir plus the style guide tree.
run_review_agent() {
    local project_root="$1" prompt="$2" log_file="$3"
    local final_prompt="$prompt"
    case "$STYLE_AGENT_MODE" in
        claude)
            claude --print --dangerously-skip-permissions \
                --settings '{"sandbox":{"enabled":false}}' \
                -- "$final_prompt" > "$log_file" 2>&1
            ;;
        codex)
            final_prompt=$'IMPORTANT: Do NOT spawn sub-agents, delegate, or parallelize through helper agents. Complete this review yourself in a single agent run.\nIMPORTANT: Edit only the provided evaluation markdown file. Do NOT modify source code or any style guide file.\n\n'"$prompt"
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
                --add-dir "$NATE_STYLE_DIR" \
                -- "$final_prompt" \
                > "$log_file" 2>&1
            ;;
        *)
            echo "unsupported style_eval mode: $STYLE_AGENT_MODE" > "$log_file"
            return 1
            ;;
    esac
}

# Build the project list. Parallel arrays:
#   names[i]        -- project name
#   eval_files[i]   -- scratch path for that project's pending evaluation markdown
names=()
eval_files=()
project_roots=()

# Resolve each [targets] entry into (name, eval_file). <dir> or <dir>/<subpath>;
# a member's name is its last path segment. Matches the eval stage's resolution.
for entry in ${targets[@]+"${targets[@]}"}; do
    if [[ "$entry" == */* ]]; then
        name="${entry##*/}"
    else
        name="$entry"
    fi
    project_root="${RUST_DIR}/${entry}"
    if [[ -n "$SINGLE_PROJECT" && "$name" != "$SINGLE_PROJECT" ]]; then
        continue
    fi
    [[ ! -d "$project_root" ]] && continue
    [[ ! -f "$project_root/Cargo.toml" ]] && continue
    names+=("$name")
    eval_files+=("$LOG_DIR/style_eval_review_${name}_EVALUATION.md")
    project_roots+=("$project_root")
done

if [[ ${#names[@]} -eq 0 ]]; then
    echo "No projects to review."
    exit 0
fi

# Eligibility filter: pending evaluation must exist, have findings, and not yet
# carry a Review Log. Build the launch list.
launch_names=()
launch_roots=()
launch_evals=()
skipped_no_eval=0
skipped_no_findings=0
skipped_already_reviewed=0

for i in "${!names[@]}"; do
    name="${names[$i]}"
    eval_file="${eval_files[$i]}"
    project_root="${project_roots[$i]}"

    status=$(python3 "$HISTORY_HELPER" evaluation-status --project "$name" --field status 2>/dev/null || echo "missing")
    if [[ "$status" == "missing" ]]; then
        skipped_no_eval=$((skipped_no_eval + 1))
        continue
    fi
    if [[ "$status" == "no_findings" ]]; then
        skipped_no_findings=$((skipped_no_findings + 1))
        continue
    fi
    if [[ "$status" == "reviewed_findings" ]]; then
        skipped_already_reviewed=$((skipped_already_reviewed + 1))
        echo "SKIP: $name (already reviewed)"
        continue
    fi
    rm -f "$eval_file"
    if ! python3 "$HISTORY_HELPER" export-evaluation --project "$name" --output "$eval_file"; then
        echo "FAILED: $name (could not export pending evaluation)"
        skipped_no_eval=$((skipped_no_eval + 1))
        continue
    fi
    launch_names+=("$name")
    launch_roots+=("$project_root")
    launch_evals+=("$eval_file")
done

if [[ ${#launch_names[@]} -eq 0 ]]; then
    echo "No pending evaluations eligible for review."
    echo "  no eval: $skipped_no_eval"
    echo "  no findings: $skipped_no_findings"
    echo "  already reviewed: $skipped_already_reviewed"
    exit 0
fi

echo "=== Style eval review ($STYLE_AGENT_MODE): ${#launch_names[@]} projects ==="

# Launch all reviews in parallel via the configured agent (claude or codex).
pids=()
launched_names=()
launched_evals=()
for i in "${!launch_names[@]}"; do
    proj="${launch_names[$i]}"
    project_root="${launch_roots[$i]}"
    log_file="$LOG_DIR/style_eval_review_${proj}.log"

    prompt="$(sed \
        -e "s|\$ARGUMENTS|$project_root|g" \
        -e "s|\$EVALUATION_PATH|$eval_file|g" \
        "$CMD_FILE")"

    run_review_agent "$project_root" "$prompt" "$log_file" &
    pids+=($!)
    launched_names+=("$proj")
    launched_evals+=("${launch_evals[$i]}")
    echo "Launched: $proj via $STYLE_AGENT_MODE (PID $!)"
done

echo ""
echo "Waiting for ${#pids[@]} processes..."

failed=0
succeeded=0
idx=0
if [[ ${#pids[@]} -gt 0 ]]; then
for pid in "${pids[@]}"; do
    name="${launched_names[$idx]}"
    eval_file="${launched_evals[$idx]}"
    wait "$pid" && code=0 || code=$?
    if [[ $code -ne 0 ]]; then
        echo "WARN: $name (review agent exited $code)"
    fi
    if grep -q '^## Review Log$' "$eval_file" 2>/dev/null; then
        python3 "$HISTORY_HELPER" save-evaluation \
            --project-root "${launch_roots[$idx]}" \
            --evaluation "$eval_file"
        echo "OK: $name"
        succeeded=$((succeeded + 1))
    else
        echo "FAILED: $name (no Review Log appended)"
        failed=$((failed + 1))
    fi
    idx=$((idx + 1))
done
fi

echo ""
echo "=== Done: $succeeded reviewed, $failed failed out of ${#launch_names[@]} ==="
