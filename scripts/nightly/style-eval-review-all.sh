#!/bin/bash
# Run /style_eval_review on every project that has a fresh EVALUATION.md.
# Runs after style-eval-all.sh and before style-fix-worktrees.sh in the
# nightly pipeline.
#
# This stage is ALWAYS Claude. It is intentionally NOT controlled by the
# [style_eval] mode flag — codex eval can produce findings that Claude is
# better at trimming and tightening before they hand off to the fix agent.
#
# Usage: style-eval-review-all.sh [project_name]
#   If project_name is given, only review that single project's EVALUATION.md.
#   If omitted, review every eligible project's EVALUATION.md.
#
# Eligibility:
#   - EVALUATION.md exists at the project root (or workspace-member subpath)
#   - EVALUATION.md has at least one `### N.` numbered finding
#   - EVALUATION.md does not yet contain a `## Review Log` section
#     (review is idempotent — already-reviewed evals are skipped)

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_DIR="$HOME/rust"
CONF_FILE="$SCRIPT_DIR/nightly-rust.conf"
CMD_FILE="$HOME/.claude/commands/style_eval_review.md"
LOG_DIR="/private/tmp/claude"
SINGLE_PROJECT="${1:-}"

mkdir -p "$LOG_DIR"

# Parse conf for excludes + workspace members. Style_eval mode is irrelevant
# here — review is always-claude — but we still honor [exclude] and
# [workspace_members] so the project list matches the eval stage.
excludes=()
ws_names=()
ws_paths=()

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
            workspace_members)
                if [[ "$stripped" =~ ^([^=]+)=(.+)$ ]]; then
                    wm_name="${BASH_REMATCH[1]}"
                    wm_rhs="${BASH_REMATCH[2]}"
                    wm_name="${wm_name## }"
                    wm_name="${wm_name%% }"
                    wm_rhs="${wm_rhs## }"
                    wm_rhs="${wm_rhs%% }"
                    if [[ "$wm_rhs" == *":"* ]]; then
                        wm_path="${wm_rhs%%:*}"
                    else
                        wm_path="$wm_rhs"
                    fi
                    wm_path="${wm_path%/}"
                    ws_names+=("$wm_name")
                    ws_paths+=("$wm_path")
                fi
                ;;
        esac
    done < "$CONF_FILE"
fi

# Build the project list. Parallel arrays:
#   names[i]        -- project name
#   eval_files[i]   -- absolute path to that project's EVALUATION.md
names=()
eval_files=()

# Pass 1: standalone projects
for project_dir in "$RUST_DIR"/*/; do
    name=$(basename "$project_dir")
    [[ ! -f "$project_dir/Cargo.toml" ]] && continue
    [[ "$name" == *_style_fix ]] && continue
    [[ -f "$project_dir/.git" ]] && continue
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
    names+=("$name")
    eval_files+=("${project_dir%/}/EVALUATION.md")
done

# Pass 2: workspace members
for i in "${!ws_names[@]}"; do
    name="${ws_names[$i]}"
    if [[ -n "$SINGLE_PROJECT" && "$name" != "$SINGLE_PROJECT" ]]; then
        continue
    fi
    member_root="${RUST_DIR}/${ws_paths[$i]}"
    [[ ! -d "$member_root" ]] && continue
    [[ ! -f "$member_root/Cargo.toml" ]] && continue
    names+=("$name")
    eval_files+=("$member_root/EVALUATION.md")
done

if [[ ${#names[@]} -eq 0 ]]; then
    echo "No projects to review."
    exit 0
fi

# Eligibility filter: EVALUATION.md must exist, have findings, and not yet
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
    project_root="$(dirname "$eval_file")"

    if [[ ! -f "$eval_file" ]]; then
        skipped_no_eval=$((skipped_no_eval + 1))
        continue
    fi
    finding_count=$(grep -c '^### [0-9]' "$eval_file" 2>/dev/null || true)
    if [[ "$finding_count" -eq 0 ]]; then
        skipped_no_findings=$((skipped_no_findings + 1))
        continue
    fi
    if grep -q '^## Review Log$' "$eval_file" 2>/dev/null; then
        skipped_already_reviewed=$((skipped_already_reviewed + 1))
        echo "SKIP: $name (already reviewed)"
        continue
    fi
    launch_names+=("$name")
    launch_roots+=("$project_root")
    launch_evals+=("$eval_file")
done

if [[ ${#launch_names[@]} -eq 0 ]]; then
    echo "No EVALUATION.md files eligible for review."
    echo "  no eval: $skipped_no_eval"
    echo "  no findings: $skipped_no_findings"
    echo "  already reviewed: $skipped_already_reviewed"
    exit 0
fi

echo "=== Style eval review: ${#launch_names[@]} projects ==="

# Launch all reviews in parallel. Always Claude — codex is intentionally not
# wired into this stage.
pids=()
launched_names=()
launched_evals=()
for i in "${!launch_names[@]}"; do
    proj="${launch_names[$i]}"
    project_root="${launch_roots[$i]}"
    log_file="$LOG_DIR/style_eval_review_${proj}.log"

    prompt="$(sed "s|\$ARGUMENTS|$project_root|g" "$CMD_FILE")"

    claude --print --dangerously-skip-permissions \
        --settings '{"sandbox":{"enabled":false}}' \
        -- "$prompt" > "$log_file" 2>&1 &
    pids+=($!)
    launched_names+=("$proj")
    launched_evals+=("${launch_evals[$i]}")
    echo "Launched: $proj (PID $!)"
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
