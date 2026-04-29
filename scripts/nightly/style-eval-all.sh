#!/bin/bash
# Run style evaluations on all eligible Rust projects in parallel.
# Uses the same exclude list as the nightly build from nightly-rust.conf.
# Can be run standalone or called from nightly-rust-clean-build.sh.
#
# Usage: style-eval-all.sh [project_name]
#   If project_name is given, only evaluate that single project.
#   If omitted, evaluate all eligible projects under ~/rust/.
#
# Projects come from two sources:
#   1. Standalone repos under ~/rust/*/ (traditional)
#   2. [workspace_members] in nightly-rust.conf (members of a cargo workspace)

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

# Parse conf file for excludes, settings, and workspace members.
# Workspace-member entries are stored in three parallel arrays indexed by position.
excludes=()
MAX_NEW_FINDINGS=5
ws_names=()   # project_name
ws_paths=()   # workspace_dir/subpath
ws_pkgs=()    # package_name

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
                        wm_pkg="${wm_rhs#*:}"
                    else
                        wm_path="$wm_rhs"
                        wm_pkg=""
                    fi
                    wm_path="${wm_path%/}"
                    if [[ -z "$wm_pkg" ]]; then
                        wm_pkg="${wm_path##*/}"
                    fi
                    ws_names+=("$wm_name")
                    ws_paths+=("$wm_path")
                    ws_pkgs+=("$wm_pkg")
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

# Build project list. Parallel arrays indexed by position:
#   projects[i]             -- project name
#   project_roots[i]        -- absolute path to project root (for $ARGUMENTS)
#   project_worktree_evals[i] -- EVALUATION.md path inside the style-fix worktree
projects=()
project_roots=()
project_worktree_evals=()

# Pass 1: standalone projects from directory scan
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

    projects+=("$name")
    project_roots+=("${RUST_DIR}/${name}")
    project_worktree_evals+=("${RUST_DIR}/${name}_style_fix/EVALUATION.md")
done

# Pass 2: workspace members from conf
for i in "${!ws_names[@]}"; do
    name="${ws_names[$i]}"
    if [[ -n "$SINGLE_PROJECT" && "$name" != "$SINGLE_PROJECT" ]]; then
        continue
    fi
    wm_path="${ws_paths[$i]}"
    member_root="${RUST_DIR}/${wm_path}"
    if [[ ! -d "$member_root" ]]; then
        echo "SKIP: $name (member path not found: $member_root)"
        continue
    fi
    if [[ ! -f "$member_root/Cargo.toml" ]]; then
        echo "SKIP: $name (no Cargo.toml at $member_root)"
        continue
    fi
    subpath="${wm_path#*/}"
    projects+=("$name")
    project_roots+=("$member_root")
    project_worktree_evals+=("${RUST_DIR}/${name}_style_fix/${subpath}/EVALUATION.md")
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
for i in "${!projects[@]}"; do
    proj="${projects[$i]}"
    project_root="${project_roots[$i]}"
    worktree_eval="${project_worktree_evals[$i]}"

    existing_findings=0
    if [[ -f "$project_root/EVALUATION.md" ]]; then
        existing_findings=$(grep -c '^### [0-9]' "$project_root/EVALUATION.md" 2>/dev/null || true)
    fi
    if [[ "$existing_findings" -ge "$MAX_NEW_FINDINGS" ]]; then
        echo "SKIP: $proj (already at cap of $MAX_NEW_FINDINGS findings)"
        continue
    fi

    # Incremental skip: if EVALUATION.md exists and neither the project source
    # nor the style guide tree has been touched since EVALUATION.md was written,
    # last night's findings are still authoritative — no need to re-eval. Use
    # mtime-based comparison against EVALUATION.md as the "last eval" timestamp.
    eval_md="$project_root/EVALUATION.md"
    if [[ -f "$eval_md" ]]; then
        nate_style_dir="$HOME/rust/nate_style/rust"
        # Candidate paths that would invalidate the cache. find's stderr is
        # silenced so missing dirs (e.g. no examples/, no tests/) don't surface.
        source_changed=$(find \
            "$project_root/src" \
            "$project_root/examples" \
            "$project_root/tests" \
            "$project_root/Cargo.toml" \
            -newer "$eval_md" -type f -print -quit 2>/dev/null || true)
        guideline_changed=$(find \
            "$nate_style_dir" \
            "$project_root/docs/style" \
            -newer "$eval_md" -type f -print -quit 2>/dev/null || true)
        if [[ -z "$source_changed" && -z "$guideline_changed" ]]; then
            eval_ts=$(stat -f '%Sm' -t '%Y-%m-%dT%H:%M:%SZ' "$eval_md" 2>/dev/null || stat -c '%y' "$eval_md" 2>/dev/null)
            echo "SKIP: $proj (UNCHANGED — no source or guideline edits since EVALUATION.md@${eval_ts})"
            continue
        fi
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
            -e "s|\$WORKTREE_EVAL_PATH|$worktree_eval|g" \
            "$CMD_FILE"
    )"
    run_style_agent "$project_root" "$prompt" "$LOG_DIR/style_eval_${proj}.log" &
    pids+=($!)
    names+=("$proj")
    roots_for_wait+=("$project_root")
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
    project_root="${roots_for_wait[$idx]}"
    wait "$pid" && code=0 || code=$?
    if [[ ! -f "$project_root/EVALUATION.md" ]]; then
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
    if [[ $(grep -c '^### [0-9]' "$project_root/EVALUATION.md" || true) -eq 0 ]]; then
        python3 "$HISTORY_HELPER" finalize-no-findings --project "$name" || {
            echo "FAILED: $name (could not finalize no-findings history)"
            failed=$((failed + 1))
            idx=$((idx + 1))
            continue
        }
    fi
    lines=$(wc -l < "$project_root/EVALUATION.md")
    echo "OK: $name ($lines lines)"
    succeeded=$((succeeded + 1))
    idx=$((idx + 1))
done

echo ""
echo "=== Done: $succeeded succeeded, $failed failed out of ${#projects[@]} ==="
