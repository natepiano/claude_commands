#!/bin/bash
# Run style evaluations on all eligible Rust projects in parallel.
# Uses the same exclude list as the nightly build from nightly-rust.conf.
# Can be run standalone or called from nightly-rust-clean-build.sh.

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_DIR="$HOME/rust"
CONF_FILE="$SCRIPT_DIR/nightly-rust.conf"
CMD_FILE="$HOME/.claude/commands/style_eval.md"
LOG_DIR="/private/tmp/claude"

mkdir -p "$LOG_DIR"

# Parse exclude list from conf file
excludes=()
if [[ -f "$CONF_FILE" ]]; then
    in_exclude=false
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="${stripped## }"
        stripped="${stripped%% }"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            [[ "${BASH_REMATCH[1]}" == "exclude" ]] && in_exclude=true || in_exclude=false
            continue
        fi
        $in_exclude && excludes+=("$stripped")
    done < "$CONF_FILE"
fi

# Build project list
projects=()
for project_dir in "$RUST_DIR"/*/; do
    name=$(basename "$project_dir")
    [[ ! -f "$project_dir/Cargo.toml" ]] && continue
    [[ "$name" == *_style_fix ]] && continue

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
    prompt="$(sed "s|\$ARGUMENTS|${RUST_DIR}/${proj}|g" "$CMD_FILE")"
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
