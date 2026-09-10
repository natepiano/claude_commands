#!/usr/bin/env bash
# Run the style-eval-review prompt on every project with fresh pending evaluation markdown.
# Runs after style-eval-all.sh and before style-fix-worktrees.sh in the
# fix pipeline.
#
# The review agent resolves from the fix.style_eval_review row in
# config/agents.conf. The prompt lives at style-eval-review-prompt.md in this
# directory — it is not a slash command.
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

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.10.
PY="$SCRIPT_DIR/../lib/py"
source "$SCRIPT_DIR/agent_assignments.sh"

RUST_DIR="$HOME/rust"
NATE_STYLE_DIR="$HOME/rust/nate_style"
CONF_FILE="$SCRIPT_DIR/fix.conf"
CMD_FILE="$SCRIPT_DIR/style-eval-review-prompt.md"
HISTORY_HELPER="$SCRIPT_DIR/style_history.py"
# /tmp/claude, not /private/tmp/claude: on macOS /tmp *is* /private/tmp —
# same directory, same inode — while on Linux /private does not exist and
# cannot be created, so the old constant killed these scripts at the first
# mkdir under `set -e`. One constant is correct on both platforms.
LOG_DIR="/tmp/claude"
SINGLE_PROJECT="${1:-}"
STYLE_ENABLED=""
STYLE_AGENT=""
STYLE_AGENT_MODEL=""
STYLE_AGENT_EFFORT=""
CODEX_BIN="${CODEX_BIN:-$(command -v codex 2>/dev/null || echo "$HOME/.local/bin/codex")}"

mkdir -p "$LOG_DIR"

# Parse conf for the [projects] allowlist so the review project list matches the
# eval stage. Agent assignment resolves from config/agents.conf through the
# registry; agent-assignments.conf carries only enabled=.
projects=()
cf_ac_keys=()

# A /fix skip comments the [projects] line out with this marker, so a skipped
# entry is invisible to the parse loop below. Capture those separately: naming a
# project explicitly overrides its skip (see the resurrection block after the
# parse), while the unnamed all-projects form still honors it.
FIX_SKIP_RE='^[[:space:]]*#FIX_SKIP#[[:space:]]+(.+)$'
skipped_entries=()
cf_ac_vals=()

if [[ -f "$CONF_FILE" ]]; then
    current_section=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$current_section" == "projects" && "$line" =~ $FIX_SKIP_RE ]]; then
            skipped_entries+=("$(cf_trim "${BASH_REMATCH[1]}")")
            continue
        fi
        stripped="${line%%#*}"
        stripped="$(cf_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            current_section="${BASH_REMATCH[1]}"
            continue
        fi
        case "$current_section" in
            projects) projects+=("$stripped") ;;
            active_checkout)
                cf_ac_keys+=("$(cf_trim "${stripped%%=*}")")
                cf_ac_vals+=("$(cf_trim "${stripped#*=}")")
                ;;
            style_eval)
                if [[ "$stripped" =~ ^mode= ]]; then
                    echo "ERROR: [style_eval] stale fix setting; stage enablement lives in $FIX_AGENT_ASSIGNMENTS_FILE and agent settings live in $AGENTS_CONFIG_FILE" >&2
                    exit 1
                elif [[ "$stripped" =~ ^(enabled|agent|model|effort)= ]]; then
                    echo "ERROR: [style_eval] stale fix setting; stage enablement lives in $FIX_AGENT_ASSIGNMENTS_FILE and agent settings live in $AGENTS_CONFIG_FILE" >&2
                    exit 1
                fi
                ;;
        esac
    done < "$CONF_FILE"
fi

# An explicitly named project overrides a temporary /fix skip — naming it is the
# intent. An unmatched name is an error rather than a silent empty run, so a typo
# does not read the same as "nothing to do".
if [[ -n "$SINGLE_PROJECT" ]]; then
    for skipped in ${skipped_entries[@]+"${skipped_entries[@]}"}; do
        if [[ "${skipped##*/}" == "$SINGLE_PROJECT" ]]; then
            echo "NOTE: $SINGLE_PROJECT is temporarily skipped; running it because it was named explicitly."
            projects+=("$skipped")
        fi
    done
    single_found=0
    for entry in ${projects[@]+"${projects[@]}"}; do
        if [[ "${entry##*/}" == "$SINGLE_PROJECT" ]]; then
            single_found=1
        fi
    done
    if (( ! single_found )); then
        echo "ERROR: no [projects] entry named '$SINGLE_PROJECT' in $CONF_FILE" >&2
        exit 1
    fi
fi

# Review has its own stage assignment. Empty model/effort values are filled from
# the global agent registry before launch.
cf_load_stage_assignment style_eval_review STYLE_ENABLED STYLE_AGENT STYLE_AGENT_MODEL STYLE_AGENT_EFFORT || exit 1
# Stage enablement is scheduled-run policy: only the scheduled run (which sets
# FIX_SCHEDULED=1 via fix-trigger.sh) consults it. A standalone invocation runs.
if [[ "${FIX_SCHEDULED:-0}" == "1" && "$STYLE_ENABLED" == "false" ]]; then
    echo "Style evaluation review is disabled for scheduled runs."
    exit 0
fi
if [[ "$STYLE_AGENT" == "codex" && ! -x "$CODEX_BIN" ]]; then
    echo "ERROR: configured Codex binary is not executable: $CODEX_BIN" >&2
    exit 1
fi

# Substitute $ARGUMENTS into the prompt, then invoke the configured review agent.
# Mirrors run_style_agent in style-eval-all.sh; the review only edits
# evaluation markdown, so codex gets the workspace dir plus the style guide tree.
run_review_agent() {
    local project_root="$1" prompt="$2" log_file="$3"
    local final_prompt="$prompt"
    case "$STYLE_AGENT" in
        claude)
            local claude_args=()
            if [[ -n "$STYLE_AGENT_MODEL" ]]; then
                claude_args+=("--model" "$STYLE_AGENT_MODEL")
            fi
            if [[ -n "$STYLE_AGENT_EFFORT" ]]; then
                claude_args+=("--effort" "$STYLE_AGENT_EFFORT")
            fi
            claude --print --dangerously-skip-permissions \
                --settings '{"sandbox":{"enabled":false}}' \
                ${claude_args[@]+"${claude_args[@]}"} \
                -- "$final_prompt" > "$log_file" 2>&1
            ;;
        codex)
            final_prompt=$'IMPORTANT: Do NOT spawn sub-agents, delegate, or parallelize through helper agents. Complete this review yourself in a single agent run.\nIMPORTANT: Edit only the provided evaluation markdown file. Do NOT modify source code or any style guide file.\n\n'"$prompt"
            local codex_args=()
            if [[ -n "$STYLE_AGENT_MODEL" ]]; then
                codex_args+=("-m" "$STYLE_AGENT_MODEL")
            fi
            if [[ -n "$STYLE_AGENT_EFFORT" ]]; then
                codex_args+=("-c" "model_reasoning_effort=\"$STYLE_AGENT_EFFORT\"")
            fi
            "$CODEX_BIN" exec \
                ${codex_args[@]+"${codex_args[@]}"} \
                --ephemeral \
                -s workspace-write \
                -C "$project_root" \
                --add-dir "$NATE_STYLE_DIR" \
                -- "$final_prompt" \
                > "$log_file" 2>&1
            ;;
        *)
            echo "unsupported style_eval agent: $STYLE_AGENT" > "$log_file"
            return 1
            ;;
    esac
}

has_real_style_fix_worktree() {
    local project="$1" project_root="$2"
    local worktree_dir="$RUST_DIR/${project}_style_fix"
    [[ -d "$worktree_dir" ]] || return 1
    git -C "$worktree_dir" rev-parse --git-dir >/dev/null 2>&1 || return 1
    git -C "$project_root" worktree list --porcelain 2>/dev/null \
        | grep -qF "worktree $worktree_dir"
}

# Build the project list. Parallel arrays:
#   names[i]        -- project name
#   eval_files[i]   -- scratch path for that project's pending evaluation markdown
names=()
eval_files=()
project_roots=()

# Resolve each [projects] entry into (name, eval_file). <dir> or <dir>/<subpath>;
# a member's name is its last path segment. project_root comes from the checkout
# (an [active_checkout] redirect may point it at a worktree). Matches the eval
# stage's resolution.
for entry in ${projects[@]+"${projects[@]}"}; do
    if [[ "$entry" == */* ]]; then
        name="${entry##*/}"
    else
        name="$entry"
    fi
    checkout="$(cf_resolve_checkout "$entry")"
    project_root="${RUST_DIR}/${checkout}"
    if [[ -n "$SINGLE_PROJECT" && "$name" != "$SINGLE_PROJECT" ]]; then
        continue
    fi
    [[ ! -d "$project_root" ]] && continue
    [[ ! -f "$project_root/Cargo.toml" ]] && continue
    names+=("$name")
    eval_files+=("$LOG_DIR/style_eval_review_${name}_evaluation.md")
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
skipped_style_fix_worktree=0

for i in "${!names[@]}"; do
    name="${names[$i]}"
    eval_file="${eval_files[$i]}"
    project_root="${project_roots[$i]}"

    if has_real_style_fix_worktree "$name" "$project_root"; then
        skipped_style_fix_worktree=$((skipped_style_fix_worktree + 1))
        echo "SKIP: $name (style_fix worktree; preserving pending handoff)"
        continue
    fi

    status=$("$PY" "$HISTORY_HELPER" evaluation-status --project "$name" --field status 2>/dev/null || echo "missing")
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
    if [[ "$status" == "fixed_findings" || "$status" == "fix_failed_findings" ]]; then
        skipped_already_reviewed=$((skipped_already_reviewed + 1))
        echo "SKIP: $name ($status)"
        continue
    fi
    rm -f "$eval_file"
    if ! "$PY" "$HISTORY_HELPER" export-evaluation --project "$name" --kind review --output "$eval_file"; then
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
    echo "  style_fix worktree: $skipped_style_fix_worktree"
    exit 0
fi

echo "=== Style eval review ($STYLE_AGENT): ${#launch_names[@]} projects ==="

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
        -e "s|\$EVALUATION_PATH|${launch_evals[$i]}|g" \
        "$CMD_FILE")"

    run_review_agent "$project_root" "$prompt" "$log_file" &
    pids+=($!)
    launched_names+=("$proj")
    launched_evals+=("${launch_evals[$i]}")
    echo "Launched: $proj via $STYLE_AGENT (PID $!)"
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
        "$PY" "$HISTORY_HELPER" save-evaluation \
            --project-root "${launch_roots[$idx]}" \
            --evaluation "$eval_file"
        rm -f "$eval_file"
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
