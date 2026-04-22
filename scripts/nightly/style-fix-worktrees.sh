#!/bin/bash
# Create style-fix worktrees for projects with EVALUATION.md findings.
# For each eligible project: create a worktree, launch the configured style agent to apply fixes + clippy.
# Uses the same exclude list as the nightly build from nightly-rust.conf.
# Can be run standalone or called from nightly-rust-clean-build.sh.
#
# Usage: style-fix-worktrees.sh [project_name]
#   If project_name is given, only process that single project.
#   If omitted, process all eligible projects under ~/rust/.
#
# Projects come from two sources:
#   1. Standalone repos under ~/rust/*/ (traditional)
#   2. [workspace_members] in nightly-rust.conf (members of a cargo workspace)

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
source "$HOME/.cargo/env"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_DIR="$HOME/rust"
CONF_FILE="$SCRIPT_DIR/nightly-rust.conf"
HISTORY_HELPER="$SCRIPT_DIR/style_history.py"
LOG_DIR="/private/tmp/claude"
SINGLE_PROJECT="${1:-}"
STYLE_AGENT_MODE="claude"

mkdir -p "$LOG_DIR"

# Parse conf file for excludes, settings, and workspace members
excludes=()
MAX_NEW_FINDINGS=5
ws_names=()
ws_paths=()
ws_pkgs=()

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
            final_prompt=$'IMPORTANT: Do NOT spawn sub-agents, delegate, or parallelize through helper agents. Complete this fix pass yourself in a single agent run.\nIMPORTANT: Do NOT create, replace, repair, or symlink the workspace path or any parent/peer repo path. If the expected workspace path is missing or invalid, fail and report it instead of trying to reconstruct it.\n\n'"$prompt"
            codex exec \
                -c model_reasoning_effort='"high"' \
                --ephemeral \
                --full-auto \
                -C "$project_root" \
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

# Collect candidate project names from both sources
candidates=()
candidate_is_ws_idx=()  # for each candidate, the index into ws_names[] if it came from there, or -1

for project_dir in "$RUST_DIR"/*/; do
    name=$(basename "$project_dir")
    [[ ! -f "$project_dir/Cargo.toml" ]] && continue
    candidates+=("$name")
    candidate_is_ws_idx+=("-1")
done

for i in "${!ws_names[@]}"; do
    candidates+=("${ws_names[$i]}")
    candidate_is_ws_idx+=("$i")
done

# Eligibility pass. Parallel arrays:
#   eligible[] -- project names
#   records[]  -- fields joined by ASCII Unit Separator (0x1F):
#     name | kind | repo_dir | eval_file | worktree_dir | worktree_eval | subpath | pkg | branch
# Using 0x1F (not tab) so that empty fields in the middle (standalone projects
# have empty subpath and pkg) survive `read` without being collapsed by
# IFS whitespace-consolidation.
RS=$'\x1f'
eligible=()
records=()
skipped=0

for ci in "${!candidates[@]}"; do
    name="${candidates[$ci]}"
    ws_idx="${candidate_is_ws_idx[$ci]}"

    if [[ -n "$SINGLE_PROJECT" && "$name" != "$SINGLE_PROJECT" ]]; then
        continue
    fi

    if [[ "$ws_idx" != "-1" ]]; then
        # Workspace member
        kind="workspace_member"
        wm_path="${ws_paths[$ws_idx]}"
        pkg="${ws_pkgs[$ws_idx]}"
        subpath="${wm_path#*/}"
        workspace_dir_name="${wm_path%%/*}"
        repo_dir="${RUST_DIR}/${workspace_dir_name}"
        member_dir="${RUST_DIR}/${wm_path}"
        eval_file="${member_dir}/EVALUATION.md"
        worktree_dir="${RUST_DIR}/${name}_style_fix"
        worktree_eval="${worktree_dir}/${subpath}/EVALUATION.md"
        branch_name="refactor/style/${name}"

        if [[ ! -d "$repo_dir/.git" ]]; then
            echo "SKIP: $name (workspace repo not a git repo: $repo_dir)"
            skipped=$((skipped + 1))
            continue
        fi
        if [[ ! -d "$member_dir" ]]; then
            echo "SKIP: $name (member path not found: $member_dir)"
            skipped=$((skipped + 1))
            continue
        fi
    else
        # Standalone project
        project_dir="${RUST_DIR}/${name}"

        if [[ "$name" == *_style_fix ]]; then
            echo "SKIP: $name (style-fix worktree)"
            skipped=$((skipped + 1))
            continue
        fi

        if [[ -f "$project_dir/.git" ]]; then
            echo "SKIP: $name (worktree, not primary checkout)"
            skipped=$((skipped + 1))
            continue
        fi

        skip=false
        for exclude in "${excludes[@]}"; do
            if [[ "$name" == "$exclude" ]]; then
                skip=true
                break
            fi
        done
        if $skip; then
            echo "SKIP: $name (excluded)"
            skipped=$((skipped + 1))
            continue
        fi

        kind="standalone"
        pkg=""
        subpath=""
        repo_dir="$project_dir"
        member_dir="$project_dir"
        eval_file="$project_dir/EVALUATION.md"
        worktree_dir="${RUST_DIR}/${name}_style_fix"
        worktree_eval="${worktree_dir}/EVALUATION.md"
        branch_name="refactor/style"
    fi

    # Check 0: EVALUATION.md exists with numbered findings
    finding_count=0
    if [[ -f "$eval_file" ]]; then
        finding_count=$(grep -c '^### [0-9]' "$eval_file" 2>/dev/null || true)
    fi
    if [[ ! -f "$eval_file" ]] || [[ "$finding_count" -eq 0 ]]; then
        if [[ -n "$SINGLE_PROJECT" ]]; then
            echo "EVAL: $name (no evaluation, running style eval...)"
            "$SCRIPT_DIR/style-eval-all.sh" "$name"
            if [[ ! -f "$eval_file" ]]; then
                echo "SKIP: $name (eval produced no EVALUATION.md)"
                skipped=$((skipped + 1))
                continue
            fi
            finding_count=$(grep -c '^### [0-9]' "$eval_file" 2>/dev/null || true)
        else
            echo "SKIP: $name (no EVALUATION.md or no findings)"
            skipped=$((skipped + 1))
            continue
        fi
    fi
    if [[ "$finding_count" -eq 0 ]]; then
        echo "SKIP: $name (eval produced no findings)"
        skipped=$((skipped + 1))
        continue
    fi

    # Check A: The target _style_fix path must be free.
    if [[ -d "$worktree_dir" ]]; then
        echo "SKIP: $name (style_fix directory exists)"
        python3 "$HISTORY_HELPER" discard-pending --project "$name" 2>/dev/null || true
        skipped=$((skipped + 1))
        continue
    fi

    # If Git still has stale metadata for a missing style-fix worktree, prune it first.
    if git -C "$repo_dir" worktree list 2>/dev/null | grep -Fq "$worktree_dir"; then
        git -C "$repo_dir" worktree prune 2>/dev/null || true
        if git -C "$repo_dir" worktree list 2>/dev/null | grep -Fq "$worktree_dir"; then
            echo "SKIP: $name (style_fix worktree registered at target path)"
            python3 "$HISTORY_HELPER" discard-pending --project "$name" 2>/dev/null || true
            skipped=$((skipped + 1))
            continue
        fi
    fi

    # Check A2: For standalone projects, no other worktree can exist on the primary repo.
    # For workspace_member projects, other worktrees of the workspace are expected
    # (concurrent style-fix worktrees for sibling members live off the same repo).
    if [[ "$kind" == "standalone" ]]; then
        worktree_count=$(git -C "$repo_dir" worktree list --porcelain 2>/dev/null | grep -c '^worktree ' || true)
        if [[ "$worktree_count" -gt 1 ]]; then
            echo "SKIP: $name (another worktree checkout already exists)"
            python3 "$HISTORY_HELPER" discard-pending --project "$name" 2>/dev/null || true
            skipped=$((skipped + 1))
            continue
        fi
    fi

    # Check B: The primary checkout is clean.
    # For workspace_member, scope the dirty check to the member subpath.
    if [[ "$kind" == "workspace_member" ]]; then
        dirty=$(git -C "$repo_dir" status --porcelain -- "$subpath" 2>/dev/null || true)
    else
        dirty=$(git -C "$repo_dir" status --porcelain 2>/dev/null || true)
    fi
    if [[ -n "$dirty" ]]; then
        echo "SKIP: $name (working tree dirty)"
        python3 "$HISTORY_HELPER" discard-pending --project "$name" 2>/dev/null || true
        skipped=$((skipped + 1))
        continue
    fi

    # Record eligibility. Delimited with 0x1F (unit separator) so empty middle
    # fields (subpath/pkg for standalone projects) survive `read` intact.
    record="${name}${RS}${kind}${RS}${repo_dir}${RS}${eval_file}${RS}${worktree_dir}${RS}${worktree_eval}${RS}${subpath}${RS}${pkg}${RS}${branch_name}"
    eligible+=("$name")
    records+=("$record")
    echo "ELIGIBLE: $name ($finding_count findings, kind=$kind)"
done

if [[ ${#eligible[@]} -eq 0 ]]; then
    echo "No projects eligible for style-fix worktrees."
    echo "=== Done: 0 created, 0 failed, $skipped skipped ==="
    exit 0
fi

echo ""
echo "=== Style-fix worktrees: ${#eligible[@]} eligible projects ==="

RUN_DIR="$LOG_DIR/style_run_$(date +%s)_$$"
mkdir -p "$RUN_DIR"

# Look up the record for a project name.
# Sets globals: R_name R_kind R_repo_dir R_eval_file R_worktree_dir R_worktree_eval R_subpath R_pkg R_branch
load_record() {
    local target="$1"
    local i
    for i in "${!eligible[@]}"; do
        if [[ "${eligible[$i]}" == "$target" ]]; then
            IFS="$RS" read -r R_name R_kind R_repo_dir R_eval_file R_worktree_dir R_worktree_eval R_subpath R_pkg R_branch <<< "${records[$i]}"
            return 0
        fi
    done
    return 1
}

# Per-project function: create worktree and launch the configured style agent
create_and_fix() {
    local proj="$1"
    load_record "$proj" || { echo "ERROR: no record for $proj"; return 1; }
    local kind="$R_kind"
    local repo_dir="$R_repo_dir"
    local eval_file="$R_eval_file"
    local worktree_dir="$R_worktree_dir"
    local worktree_eval="$R_worktree_eval"
    local subpath="$R_subpath"
    local pkg="$R_pkg"
    local branch_name="$R_branch"
    local log_file="$LOG_DIR/style_fix_${proj}.log"

    # Create worktree (-b fails if branch exists, which is intentional)
    if ! git -C "$repo_dir" worktree add -b "$branch_name" "$worktree_dir" 2>>"$log_file"; then
        echo "ERROR: $proj (worktree creation failed — branch $branch_name may already exist)"
        return 1
    fi

    # Apply canonical settings.local.json so manual review has permissions
    mkdir -p "$worktree_dir/.claude"
    cp "$HOME/.claude/templates/settings_local.json" "$worktree_dir/.claude/settings.local.json"

    # Move EVALUATION.md into worktree so primary starts fresh.
    mkdir -p "$(dirname "$worktree_eval")"
    mv "$eval_file" "$worktree_eval"

    python3 "$HISTORY_HELPER" set-phase --project "$proj" --phase fix || true

    # Scoping values for the fix prompt
    local cargo_scope_flag
    local agent_work_dir
    local review_dir_description
    if [[ "$kind" == "workspace_member" ]]; then
        cargo_scope_flag="-p $pkg"
        agent_work_dir="$worktree_dir/$subpath"
        review_dir_description="ONLY modify files under $worktree_dir/$subpath/ — this project is a single cargo package within a workspace."
    else
        cargo_scope_flag="--workspace"
        agent_work_dir="$worktree_dir"
        review_dir_description="Modify ONLY files inside $worktree_dir"
    fi

    # Build prompt for the agent
    local prompt
    prompt=$(cat <<PROMPT_EOF
You are applying automated style fixes to a Rust project.
Working directory: $agent_work_dir
Worktree root: $worktree_dir

IMPORTANT: Do NOT spawn sub-agents, delegate, or parallelize through helper agents. Complete this fix pass yourself in a single agent run.

Step 1: Load the style guide and read referenced files
Run: zsh ~/.claude/scripts/load-rust-style.sh --project-root $agent_work_dir
Then read each unique style file referenced by the findings in EVALUATION.md. Each finding includes a **Style file** field with the full path to the style guide file (e.g., ~/rust/nate_style/rust/one-use-per-line.md or a repo-local docs/style/*.md file).
Also read each style file marked [non-negotiable] in the loaded checklist, even if no finding cites it directly. Those rules apply to every fix.

Step 2: Read the evaluation
Read the file: $worktree_eval

Step 3: Apply ALL numbered findings from the evaluation.
Each evaluation run adds up to $MAX_NEW_FINDINGS new findings, but findings accumulate
across nightly runs via carry-forward. Apply every finding present.
- Read every file cited in the finding
- Apply the changes described in "Recommended pattern"
- Skip any finding whose cited files no longer exist or whose pattern no longer matches
- If applying a finding as written would violate any [non-negotiable] rule, do NOT apply that conflicting change. Preserve the non-negotiable rule, make any safe partial progress you can, and document the conflict in the Fix Summary.

Step 4: Run cargo mend and fix issues
Run: cargo mend $cargo_scope_flag --all-targets --manifest-path $worktree_dir/Cargo.toml
- If mend fails due to missing Cargo.toml or missing toolchain, report the error and skip to Step 5.
- If mend reports fixable items, run: cargo mend $cargo_scope_flag --all-targets --fix --manifest-path $worktree_dir/Cargo.toml
  - If mend --fix fails, report the error and skip to Step 5.
- If mend reports only unfixable items, note them and continue.

Step 5: Run clippy and fix any issues
Run: cargo clippy $cargo_scope_flag --all-targets --all-features --manifest-path $worktree_dir/Cargo.toml -- -D warnings
If clippy reports errors or warnings, fix them. Include any unfixable mend items from Step 4.

Step 6: Run tests and fix any failures
Run: cargo nextest run $cargo_scope_flag --manifest-path $worktree_dir/Cargo.toml
If any tests fail, fix them.

Step 7: Style review of the diff
Run: git -C $worktree_dir diff | grep '^+' | grep -v '^+++' > /tmp/claude/style-review-additions.txt
If the file is empty, skip to Step 8 (fmt).

Find the === STYLE_CHECKLIST === section from the style guide output in Step 1.
For each rule in the checklist, check the additions-only diff for violations.
Fix any violations found. If no violations, move on.
For rules marked [non-negotiable], review the full diff intent, not just added lines. Reversions, deletions, or signature changes that violate a non-negotiable rule must be fixed or the conflicting finding must be marked partial/skipped with an explanation.

Step 8: Run cargo +nightly fmt
Run: cargo +nightly fmt $cargo_scope_flag --manifest-path $worktree_dir/Cargo.toml

Step 9: Write fix summary to EVALUATION.md
Append a section to the END of $worktree_eval with the following format:

---

## Fix Summary

For each numbered finding, add a line:

### Finding N: [title from finding]
**Status:** Applied | Partially applied | Skipped
**What was done:** [1-2 sentences describing the actual changes made]
**Issues:** [If partially applied or skipped, explain WHY — e.g., "file no longer exists",
"pattern didn't match", "fixing this would require removing a public API method",
"clippy's suggested fix conflicts with one-use-per-line style rule", etc.]
[Omit Issues line if status is Applied with no complications]

After all findings, add:

### Cargo Mend Changes
If cargo mend --fix was run in Step 4 and made any changes, summarize them here:
- List the files modified by cargo mend
- Describe the types of changes (e.g., "narrowed pub to pub(crate)", "shortened import paths")
- If cargo mend was skipped or found nothing to fix, say so explicitly

### Build Status
- **clippy:** pass | fail (with summary of remaining warnings/errors if fail)
- **tests:** pass | fail (with summary of failures if fail)

Fixing guidelines:
- Do NOT fix warnings by marking code as dead — remove dead code entirely
- Do NOT fix warnings by prefixing arguments/variables with _ — remove them if unused
- Non-negotiable style rules override any conflicting recommended pattern in a finding

Rules:
- $review_dir_description
- Do NOT commit anything (no git add, no git commit)
- EVALUATION.md ($worktree_eval) may ONLY be modified by appending the Fix Summary section (Step 9)
- Apply each fix completely — no partial changes
- If a finding references files that don't exist or patterns that don't match, skip that finding and document why in the Fix Summary
PROMPT_EOF
    )

    # Launch selected agent to apply fixes (60 min timeout to prevent hanging the pipeline)
    local agent_pid
    run_style_agent "$agent_work_dir" "$prompt" "$log_file" &
    agent_pid=$!

    local elapsed=0
    local timeout_secs=3600
    while kill -0 "$agent_pid" 2>/dev/null; do
        sleep 10
        elapsed=$((elapsed + 10))
        if [[ $elapsed -ge $timeout_secs ]]; then
            kill "$agent_pid" 2>/dev/null
            sleep 5
            kill -9 "$agent_pid" 2>/dev/null
            wait "$agent_pid" 2>/dev/null
            echo "TIMEOUT: $proj ($STYLE_AGENT_MODE exceeded 60 minute timeout)"
            python3 "$HISTORY_HELPER" finalize-failure --project "$proj" --reason "$STYLE_AGENT_MODE exceeded 60 minute timeout" || true
            [[ -f "$worktree_eval" ]] && mv "$worktree_eval" "$eval_file"
            git -C "$repo_dir" worktree remove "$worktree_dir" --force 2>/dev/null || rm -rf "$worktree_dir"
            git -C "$repo_dir" worktree prune 2>/dev/null || true
            if [[ "$(git -C "$repo_dir" rev-list --count "main..$branch_name" 2>/dev/null)" == "0" ]]; then
                git -C "$repo_dir" branch -D "$branch_name" 2>/dev/null || true
            else
                echo "WARN: $proj (kept $branch_name branch — has unmerged commits)"
            fi
            return 1
        fi
    done

    wait "$agent_pid" && agent_code=0 || agent_code=$?

    if [[ $agent_code -ne 0 ]]; then
        if [[ -f "$worktree_eval" ]] && rg -q '^## Fix Summary$' "$worktree_eval"; then
            echo "WARN: $proj ($STYLE_AGENT_MODE exited $agent_code, but Fix Summary was produced)"
        else
            local fail_reason
            if [[ ! -s "$log_file" ]]; then
                fail_reason="$STYLE_AGENT_MODE exited immediately with no output"
            else
                fail_reason="$STYLE_AGENT_MODE failed after producing output (see $log_file)"
            fi
            echo "ERROR: $proj ($fail_reason)"
            python3 "$HISTORY_HELPER" finalize-failure --project "$proj" --reason "$fail_reason" || true
            [[ -f "$worktree_eval" ]] && mv "$worktree_eval" "$eval_file"
            git -C "$repo_dir" worktree remove "$worktree_dir" --force 2>/dev/null || rm -rf "$worktree_dir"
            git -C "$repo_dir" worktree prune 2>/dev/null || true
            if [[ "$(git -C "$repo_dir" rev-list --count "main..$branch_name" 2>/dev/null)" == "0" ]]; then
                git -C "$repo_dir" branch -D "$branch_name" 2>/dev/null || true
            else
                echo "WARN: $proj (kept $branch_name branch — has unmerged commits)"
            fi
            return 1
        fi
    fi

    python3 "$HISTORY_HELPER" finalize-fix --project-root "$agent_work_dir" --evaluation "$worktree_eval" || {
        echo "ERROR: $proj (could not finalize history)"
        return 1
    }

    echo "OK: $proj (worktree created, fixes applied)"
    return 0
}

# Launch all eligible projects in parallel
pids=()
names=()
for proj in "${eligible[@]}"; do
    create_and_fix "$proj" &
    pids+=($!)
    names+=("$proj")
    echo "Launched: $proj (PID $!)"
done

echo ""
echo "Waiting for ${#pids[@]} processes..."

# Wait for all and track results
failed=0
succeeded=0
failed_names=()
idx=0
for pid in "${pids[@]}"; do
    name="${names[$idx]}"
    wait "$pid" && code=0 || code=$?
    if [[ $code -ne 0 ]]; then
        echo "FAILED: $name (exit $code)"
        failed=$((failed + 1))
        failed_names+=("$name")
    else
        echo "OK: $name"
        succeeded=$((succeeded + 1))
    fi
    idx=$((idx + 1))
done

# Retry failed projects sequentially
if [[ ${#failed_names[@]} -gt 0 ]]; then
    echo ""
    echo "=== Retrying ${#failed_names[@]} failed projects sequentially ==="
    for proj in "${failed_names[@]}"; do
        # Delete the branch from the failed first attempt so worktree add -b can recreate it
        if load_record "$proj"; then
            git -C "$R_repo_dir" branch -D "$R_branch" 2>/dev/null || true
        fi

        echo "RETRY: $proj"
        if create_and_fix "$proj"; then
            echo "RETRY OK: $proj"
            failed=$((failed - 1))
            succeeded=$((succeeded + 1))
        else
            echo "RETRY FAILED: $proj"
        fi
    done
fi

echo ""
echo "=== Done: $succeeded created, $failed failed, $skipped skipped out of $((${#eligible[@]} + skipped)) ==="

# Clean up per-run temp directory
rm -rf "$RUN_DIR"

# Update Obsidian summary
python3 "$SCRIPT_DIR/style_report.py" --generate 2>&1 || {
    echo "WARNING: failed to generate style reports"
}

# Commit the nightly history + report in ~/rust/nate_style only when every
# worktree fix succeeded. On any failure, leave nate_style dirty for review.
if (( failed == 0 )); then
    "$SCRIPT_DIR/commit-style-results.sh" 2>&1 || echo "WARNING: commit-style-results.sh failed"
else
    echo "SKIP commit-style-results: $failed worktree run(s) failed; leaving nate_style dirty for review"
fi
