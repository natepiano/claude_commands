#!/bin/bash
# Create style-fix worktrees for projects with EVALUATION.md findings.
# For each eligible project: create a worktree, launch Claude to apply fixes + clippy.
# Uses the same exclude list as the nightly build from nightly-rust.conf.
# Can be run standalone or called from nightly-rust-clean-build.sh.
#
# Usage: style-fix-worktrees.sh [project_name]
#   If project_name is given, only process that single project.
#   If omitted, process all eligible projects under ~/rust/.

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
source "$HOME/.cargo/env"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_DIR="$HOME/rust"
CONF_FILE="$SCRIPT_DIR/nightly-rust.conf"
HISTORY_HELPER="$SCRIPT_DIR/style_history.py"
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

# Build eligible project list with precondition checks
eligible=()
skipped=0
for project_dir in "$RUST_DIR"/*/; do
    name=$(basename "$project_dir")
    [[ ! -f "$project_dir/Cargo.toml" ]] && continue

    # If single project specified, skip all others
    if [[ -n "$SINGLE_PROJECT" && "$name" != "$SINGLE_PROJECT" ]]; then
        continue
    fi

    # Skip automation worktrees (prevent recursion)
    if [[ "$name" == *_style_fix ]]; then
        echo "SKIP: $name (style-fix worktree)"
        skipped=$((skipped + 1))
        continue
    fi

    # Skip worktree checkouts (only process primary repos)
    if [[ -f "$project_dir/.git" ]]; then
        echo "SKIP: $name (worktree, not primary checkout)"
        skipped=$((skipped + 1))
        continue
    fi

    # Skip excluded projects
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

    eval_file="$project_dir/EVALUATION.md"
    worktree_dir="$RUST_DIR/${name}_style_fix"

    # Check 0: EVALUATION.md exists with numbered findings
    if [[ ! -f "$eval_file" ]] || [[ $(grep -c '^### [0-9]' "$eval_file" 2>/dev/null || echo 0) -eq 0 ]]; then
        if [[ -n "$SINGLE_PROJECT" ]]; then
            echo "EVAL: $name (no evaluation, running style eval...)"
            "$SCRIPT_DIR/style-eval-all.sh" "$name"
            if [[ ! -f "$eval_file" ]]; then
                echo "SKIP: $name (eval produced no EVALUATION.md)"
                skipped=$((skipped + 1))
                continue
            fi
        else
            echo "SKIP: $name (no EVALUATION.md or no findings)"
            skipped=$((skipped + 1))
            continue
        fi
    fi
    finding_count=$(grep -c '^### [0-9]' "$eval_file" || true)
    if [[ "$finding_count" -eq 0 ]]; then
        echo "SKIP: $name (eval produced no findings)"
        skipped=$((skipped + 1))
        continue
    fi

    # Check A: The target _style_fix path must be free.
    # Other linked worktrees are allowed; only the style-fix target itself blocks creation.
    if [[ -d "$worktree_dir" ]]; then
        echo "SKIP: $name (style_fix directory exists)"
        skipped=$((skipped + 1))
        continue
    fi

    # If Git still has stale metadata for a missing style-fix worktree, prune it first.
    if git -C "$project_dir" worktree list 2>/dev/null | grep -Fq "$worktree_dir"; then
        git -C "$project_dir" worktree prune 2>/dev/null || true
        if git -C "$project_dir" worktree list 2>/dev/null | grep -Fq "$worktree_dir"; then
            echo "SKIP: $name (style_fix worktree registered at target path)"
            skipped=$((skipped + 1))
            continue
        fi
    fi

    # Check A2: Any other auxiliary worktree checkout blocks nightly fixes for this project.
    worktree_count=$(git -C "$project_dir" worktree list --porcelain 2>/dev/null | grep -c '^worktree ' || true)
    if [[ "$worktree_count" -gt 1 ]]; then
        echo "SKIP: $name (another worktree checkout already exists)"
        skipped=$((skipped + 1))
        continue
    fi

    # Check B: The primary checkout is clean.
    dirty=$(git -C "$project_dir" status --porcelain 2>/dev/null || true)
    if [[ -n "$dirty" ]]; then
        echo "SKIP: $name (working tree dirty)"
        skipped=$((skipped + 1))
        continue
    fi

    eligible+=("$name")
    echo "ELIGIBLE: $name ($finding_count findings)"
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

# Per-project function: create worktree and launch Claude to apply fixes
create_and_fix() {
    local proj="$1"
    local project_dir="$RUST_DIR/$proj"
    local worktree_dir="$RUST_DIR/${proj}_style_fix"
    local eval_file="$project_dir/EVALUATION.md"
    local log_file="$LOG_DIR/style_fix_${proj}.log"

    # Create worktree (-b fails if branch exists, which is intentional)
    if ! git -C "$project_dir" worktree add -b refactor/style "$worktree_dir" 2>>"$log_file"; then
        echo "ERROR: $proj (worktree creation failed — branch refactor/style may already exist)"
        return 1
    fi

    # Apply canonical settings.local.json so manual review has permissions
    mkdir -p "$worktree_dir/.claude"
    cp "$HOME/.claude/templates/settings_local.json" "$worktree_dir/.claude/settings.local.json"

    # Move EVALUATION.md into worktree so primary starts fresh
    mv "$eval_file" "$worktree_dir/EVALUATION.md"

    # Build prompt for Claude
    local prompt
    prompt=$(cat <<PROMPT_EOF
You are applying automated style fixes to a Rust project.
Working directory: $worktree_dir

Step 1: Load the style guide and read referenced files
Run: zsh ~/.claude/scripts/load-rust-style.sh --project-root $worktree_dir
Then read each unique style file referenced by the findings in EVALUATION.md. Each finding includes a **Style file** field with the full path to the style guide file (e.g., ~/rust/nate_style/rust/one-use-per-line.md or a repo-local docs/style/*.md file).
Also read each style file marked [non-negotiable] in the loaded checklist, even if no finding cites it directly. Those rules apply to every fix.

Step 2: Read the evaluation
Read the file: $worktree_dir/EVALUATION.md

Step 3: Apply ALL numbered findings from the evaluation.
Each evaluation run adds up to $MAX_NEW_FINDINGS new findings, but findings accumulate
across nightly runs via carry-forward. Apply every finding present.
- Read every file cited in the finding
- Apply the changes described in "Recommended pattern"
- Skip any finding whose cited files no longer exist or whose pattern no longer matches
- If applying a finding as written would violate any [non-negotiable] rule, do NOT apply that conflicting change. Preserve the non-negotiable rule, make any safe partial progress you can, and document the conflict in the Fix Summary.

Step 4: Run cargo mend and fix issues
Run: cargo mend --workspace --all-targets --manifest-path $worktree_dir/Cargo.toml
- If mend fails due to missing Cargo.toml or missing toolchain, report the error and skip to Step 5.
- If mend reports fixable items, run: cargo mend --workspace --all-targets --fix --manifest-path $worktree_dir/Cargo.toml
  - If mend --fix fails, report the error and skip to Step 5.
- If mend reports only unfixable items, note them and continue.

Step 5: Run clippy and fix any issues
Run: cargo clippy --workspace --all-targets --all-features --manifest-path $worktree_dir/Cargo.toml -- -D warnings
If clippy reports errors or warnings, fix them. Include any unfixable mend items from Step 4.

Step 6: Run tests and fix any failures
Run: cargo nextest run --workspace --manifest-path $worktree_dir/Cargo.toml
If any tests fail, fix them.

Step 7: Style review of the diff
Run: git -C $worktree_dir diff | grep '^+' | grep -v '^+++' > /tmp/claude/style-review-additions.txt
If the file is empty, skip to Step 8 (fmt).

Find the === STYLE_CHECKLIST === section from the style guide output in Step 1.
For each rule in the checklist, check the additions-only diff for violations.
Fix any violations found. If no violations, move on.
For rules marked [non-negotiable], review the full diff intent, not just added lines. Reversions, deletions, or signature changes that violate a non-negotiable rule must be fixed or the conflicting finding must be marked partial/skipped with an explanation.

Step 8: Run cargo +nightly fmt
Run: cargo +nightly fmt --all --manifest-path $worktree_dir/Cargo.toml

Step 9: Write fix summary to EVALUATION.md
Append a section to the END of $worktree_dir/EVALUATION.md with the following format:

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
- Modify ONLY files inside $worktree_dir
- Do NOT commit anything (no git add, no git commit)
- EVALUATION.md may ONLY be modified by appending the Fix Summary section (Step 8)
- Apply each fix completely — no partial changes
- If a finding references files that don't exist or patterns that don't match, skip that finding and document why in the Fix Summary
PROMPT_EOF
    )

    # Launch Claude to apply fixes (60 min timeout to prevent hanging the pipeline)
    local claude_pid
    claude --print --dangerously-skip-permissions --settings '{"sandbox":{"enabled":false}}' -- "$prompt" > "$log_file" 2>&1 &
    claude_pid=$!

    local elapsed=0
    local timeout_secs=3600
    while kill -0 "$claude_pid" 2>/dev/null; do
        sleep 10
        elapsed=$((elapsed + 10))
        if [[ $elapsed -ge $timeout_secs ]]; then
            kill "$claude_pid" 2>/dev/null
            sleep 5
            kill -9 "$claude_pid" 2>/dev/null
            wait "$claude_pid" 2>/dev/null
            echo "TIMEOUT: $proj (claude exceeded 60 minute timeout)"
            python3 "$HISTORY_HELPER" finalize-failure --project "$proj" --reason "claude exceeded 60 minute timeout" || true
            [[ -f "$worktree_dir/EVALUATION.md" ]] && mv "$worktree_dir/EVALUATION.md" "$eval_file"
            git -C "$project_dir" worktree remove "$worktree_dir" --force 2>/dev/null || rm -rf "$worktree_dir"
            git -C "$project_dir" worktree prune 2>/dev/null || true
            return 1
        fi
    done

    if ! wait "$claude_pid"; then
        # Determine failure reason from log content
        local fail_reason
        if [[ ! -s "$log_file" ]]; then
            fail_reason="claude exited immediately with no output"
        else
            fail_reason="claude failed after producing output (see $log_file)"
        fi
        echo "ERROR: $proj ($fail_reason)"
        python3 "$HISTORY_HELPER" finalize-failure --project "$proj" --reason "$fail_reason" || true
        # Cleanup: move EVALUATION.md back before removing worktree
        [[ -f "$worktree_dir/EVALUATION.md" ]] && mv "$worktree_dir/EVALUATION.md" "$eval_file"
        git -C "$project_dir" worktree remove "$worktree_dir" --force 2>/dev/null || rm -rf "$worktree_dir"
        git -C "$project_dir" worktree prune 2>/dev/null || true
        return 1
    fi

    python3 "$HISTORY_HELPER" finalize-fix --project-root "$worktree_dir" --evaluation "$worktree_dir/EVALUATION.md" || {
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
        git -C "$RUST_DIR/$proj" branch -D refactor/style 2>/dev/null || true

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
