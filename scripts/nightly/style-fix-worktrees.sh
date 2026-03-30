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
    if [[ ! -f "$eval_file" ]]; then
        echo "SKIP: $name (no EVALUATION.md)"
        skipped=$((skipped + 1))
        continue
    fi
    finding_count=$(grep -c '^### [0-9]' "$eval_file" || true)
    if [[ "$finding_count" -eq 0 ]]; then
        echo "SKIP: $name (no findings)"
        skipped=$((skipped + 1))
        continue
    fi

    # Check A: No existing style_fix worktree or directory
    if [[ -d "$worktree_dir" ]]; then
        echo "SKIP: $name (style_fix directory exists)"
        skipped=$((skipped + 1))
        continue
    fi
    if git -C "$project_dir" worktree list 2>/dev/null | grep -q "${name}_style_fix"; then
        echo "SKIP: $name (style_fix worktree registered)"
        skipped=$((skipped + 1))
        continue
    fi

    # Check B: Working tree is clean (filter out EVALUATION.md from check)
    dirty=$(git -C "$project_dir" status --porcelain 2>/dev/null | grep -v 'EVALUATION.md' || true)
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

    # Copy EVALUATION.md into worktree (keep original for carry-forward)
    cp "$eval_file" "$worktree_dir/EVALUATION.md"

    # Build prompt for Claude
    local prompt
    prompt=$(cat <<PROMPT_EOF
You are applying automated style fixes to a Rust project.
Working directory: $worktree_dir

Step 1: Load the style guide
Run this command to read the full style guide:
cat ~/rust/nate_style/rust/*.md

Step 2: Read the evaluation
Read the file: $worktree_dir/EVALUATION.md

Step 3: Apply ALL numbered findings from the evaluation.
Each evaluation run adds up to $MAX_NEW_FINDINGS new findings, but findings accumulate
across nightly runs via carry-forward. Apply every finding present.
- Read every file cited in the finding
- Apply the changes described in "Recommended pattern"
- Skip any finding whose cited files no longer exist or whose pattern no longer matches

Step 4: Run clippy and fix any issues
Run: cargo clippy --workspace --all-targets --all-features --manifest-path $worktree_dir/Cargo.toml -- -D warnings
If clippy reports errors or warnings, fix them.

Step 5: Run tests and fix any failures
Run: cargo nextest run --workspace --manifest-path $worktree_dir/Cargo.toml
If any tests fail, fix them.

Rules:
- Modify ONLY files inside $worktree_dir
- Do NOT commit anything (no git add, no git commit)
- Do NOT modify EVALUATION.md itself
- Apply each fix completely — no partial changes
- If a finding references files that don't exist or patterns that don't match, skip that finding silently
PROMPT_EOF
    )

    # Launch Claude to apply fixes
    if ! claude --print --dangerously-skip-permissions --settings '{"sandbox":{"enabled":false}}' -- "$prompt" > "$log_file" 2>&1; then
        echo "ERROR: $proj (claude fix application failed)"
        # Cleanup: remove worktree (EVALUATION.md safe in main repo since we cp'd)
        git -C "$project_dir" worktree remove "$worktree_dir" --force 2>/dev/null || true
        return 1
    fi

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
idx=0
for pid in "${pids[@]}"; do
    name="${names[$idx]}"
    wait "$pid" && code=0 || code=$?
    if [[ $code -ne 0 ]]; then
        echo "FAILED: $name (exit $code)"
        failed=$((failed + 1))
    else
        echo "OK: $name"
        succeeded=$((succeeded + 1))
    fi
    idx=$((idx + 1))
done

echo ""
echo "=== Done: $succeeded created, $failed failed, $skipped skipped out of $((${#eligible[@]} + skipped)) ==="
