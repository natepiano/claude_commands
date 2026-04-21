#!/bin/bash
# Run style evaluations on all eligible Rust projects in parallel.
# Uses the same exclude list as the nightly build from nightly-rust.conf.
# Can be run standalone or called from nightly-rust-clean-build.sh.
#
# Usage: style-eval-all.sh [project_name]
#   If project_name is given, only evaluate that single project.
#   If omitted, evaluate all eligible projects under ~/rust/.

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_DIR="$HOME/rust"
HISTORY_DIR="$HOME/rust/nate_style/.history"
CONF_FILE="$SCRIPT_DIR/nightly-rust.conf"
CMD_FILE="$HOME/.claude/commands/style_eval.md"
HISTORY_HELPER="$SCRIPT_DIR/style_history.py"
LOG_DIR="/private/tmp/claude"
SINGLE_PROJECT="${1:-}"
STYLE_AGENT_MODE="claude"

mkdir -p "$LOG_DIR"

# Parse conf file for excludes and settings
excludes=()
MAX_NEW_FINDINGS=5
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
            exclude) excludes+=("$stripped") ;;
            style_eval)
                if [[ "$stripped" =~ ^mode=(.+)$ ]]; then
                    STYLE_AGENT_MODE="${BASH_REMATCH[1]}"
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
        esac
    done < "$CONF_FILE"
fi

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
            codex exec \
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

# Build project list
projects=()
for project_dir in "$RUST_DIR"/*/; do
    name=$(basename "$project_dir")
    [[ ! -f "$project_dir/Cargo.toml" ]] && continue
    [[ "$name" == *_style_fix ]] && continue
    [[ -f "$project_dir/.git" ]] && continue

    # If single project specified, skip all others
    if [[ -n "$SINGLE_PROJECT" && "$name" != "$SINGLE_PROJECT" ]]; then
        continue
    fi

    skip=false
    for exclude in "${excludes[@]}"; do
        if [[ "$name" == "$exclude" ]]; then
            skip=true
            break
        fi
    done
    $skip && continue

    projects+=("$name")
done

if [[ ${#projects[@]} -eq 0 ]]; then
    echo "No projects to evaluate."
    exit 0
fi

echo "=== Style evaluation: ${#projects[@]} projects ==="

# Launch all evaluations in parallel
pids=()
names=()
for proj in "${projects[@]}"; do
    project_root="${RUST_DIR}/${proj}"

    existing_findings=0
    if [[ -f "$project_root/EVALUATION.md" ]]; then
        existing_findings=$(grep -c '^### [0-9]' "$project_root/EVALUATION.md" 2>/dev/null || true)
    fi
    if [[ "$existing_findings" -ge "$MAX_NEW_FINDINGS" ]]; then
        echo "SKIP: $proj (already at cap of $MAX_NEW_FINDINGS findings)"
        continue
    fi
    effective_budget=$((MAX_NEW_FINDINGS - existing_findings))

    python3 "$HISTORY_HELPER" start-run \
        --project-root "$project_root" \
        --budget "$effective_budget" || {
        echo "FAILED: $proj (could not start pending run)"
        continue
    }

    prompt="$(
        sed \
            -e "s|\$ARGUMENTS|$project_root|g" \
            "$CMD_FILE"
    )"
    run_style_agent "$project_root" "$prompt" "$LOG_DIR/style_eval_${proj}.log" &
    pids+=($!)
    names+=("$proj")
    echo "Launched: $proj via $STYLE_AGENT_MODE (PID $!)"
done

echo ""
echo "Waiting for ${#pids[@]} processes..."

# Wait for all and track results
failed=0
succeeded=0
idx=0
for pid in "${pids[@]}"; do
    name="${names[$idx]}"
    wait "$pid" && code=0 || code=$?
    if [[ ! -f "$RUST_DIR/$name/EVALUATION.md" ]]; then
        if [[ $code -ne 0 ]]; then
            echo "FAILED: $name (exit $code, no EVALUATION.md produced)"
        else
            echo "FAILED: $name (no EVALUATION.md produced)"
        fi
        python3 "$HISTORY_HELPER" discard-pending --project "$name" || true
        failed=$((failed + 1))
        idx=$((idx + 1))
        continue
    fi

    if [[ $code -ne 0 ]]; then
        echo "WARN: $name (exit $code, but EVALUATION.md was produced)"
    fi
    if [[ $(grep -c '^### [0-9]' "$RUST_DIR/$name/EVALUATION.md" || true) -eq 0 ]]; then
        python3 "$HISTORY_HELPER" finalize-no-findings --project "$name" || {
            echo "FAILED: $name (could not finalize no-findings history)"
            failed=$((failed + 1))
            idx=$((idx + 1))
            continue
        }
    fi
    lines=$(wc -l < "$RUST_DIR/$name/EVALUATION.md")
    echo "OK: $name ($lines lines)"
    succeeded=$((succeeded + 1))
    idx=$((idx + 1))
done

echo ""
echo "=== Done: $succeeded succeeded, $failed failed out of ${#projects[@]} ==="
