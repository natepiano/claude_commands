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
CONF_FILE="$SCRIPT_DIR/nightly-rust.conf"
CMD_FILE="$HOME/.claude/commands/style_eval.md"
STATE_HELPER="$SCRIPT_DIR/style_review_state.py"
LOG_DIR="/private/tmp/claude"
SINGLE_PROJECT="${1:-}"

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
                if [[ "$stripped" =~ ^max_new_findings=([0-9]+)$ ]]; then
                    MAX_NEW_FINDINGS="${BASH_REMATCH[1]}"
                fi
                ;;
        esac
    done < "$CONF_FILE"
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

# Ensure the review ledger exists before selection begins
if [[ ! -f "$HOME/rust/nate_style/usage/review_ledger.json" ]]; then
    python3 "$STATE_HELPER" bootstrap || {
        echo "FAILED: could not bootstrap review ledger"
        exit 1
    }
fi

RUN_DIR="$LOG_DIR/style_eval_run_$(date +%s)_$$"
mkdir -p "$RUN_DIR"

# Launch all evaluations in parallel
pids=()
names=()
for proj in "${projects[@]}"; do
    project_root="${RUST_DIR}/${proj}"
    manifest_path="$RUN_DIR/${proj}_selection.json"
    prior_eval_path="$RUN_DIR/${proj}_prior_evaluation.md"

    python3 "$STATE_HELPER" select \
        --project-root "$project_root" \
        --budget "$MAX_NEW_FINDINGS" \
        --output "$manifest_path" || {
        echo "FAILED: $proj (selection generation failed)"
        continue
    }

    if [[ -f "$project_root/EVALUATION.md" ]]; then
        cp "$project_root/EVALUATION.md" "$prior_eval_path"
    else
        : > "$prior_eval_path"
    fi

    prompt="$(
        sed \
            -e "s|\$ARGUMENTS|$project_root|g" \
            -e "s|__SELECTION_MANIFEST__|$manifest_path|g" \
            "$CMD_FILE"
    )"
    claude --print --dangerously-skip-permissions --settings '{"sandbox":{"enabled":false}}' -- "$prompt" > "$LOG_DIR/style_eval_${proj}.log" 2>&1 &
    pids+=($!)
    names+=("$proj")
    echo "Launched: $proj (PID $!)"
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
    if [[ $code -ne 0 ]]; then
        echo "FAILED: $name (exit $code)"
        failed=$((failed + 1))
    elif [[ -f "$RUST_DIR/$name/EVALUATION.md" ]]; then
        python3 "$STATE_HELPER" append-events \
            --project-root "$RUST_DIR/$name" \
            --manifest "$RUN_DIR/${name}_selection.json" \
            --prior-eval "$RUN_DIR/${name}_prior_evaluation.md" \
            --current-eval "$RUST_DIR/$name/EVALUATION.md" || {
            echo "FAILED: $name (could not append evaluation events)"
            failed=$((failed + 1))
            continue
        }
        lines=$(wc -l < "$RUST_DIR/$name/EVALUATION.md")
        echo "OK: $name ($lines lines)"
        succeeded=$((succeeded + 1))
    else
        echo "FAILED: $name (no EVALUATION.md produced)"
        failed=$((failed + 1))
    fi
    idx=$((idx + 1))
done

echo ""
echo "=== Done: $succeeded succeeded, $failed failed out of ${#projects[@]} ==="

rm -rf "$RUN_DIR"
