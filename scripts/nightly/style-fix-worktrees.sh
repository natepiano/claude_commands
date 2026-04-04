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

    # Check B: Working tree is clean
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

# Create per-run temp directory for isolated logging
mkdir -p ~/rust/nate_style/usage
RUN_DIR="$LOG_DIR/style_run_$(date +%s)_$$"
mkdir -p "$RUN_DIR"

# Per-project function: create worktree and launch Claude to apply fixes
create_and_fix() {
    local proj="$1"
    local project_dir="$RUST_DIR/$proj"
    local worktree_dir="$RUST_DIR/${proj}_style_fix"
    local eval_file="$project_dir/EVALUATION.md"
    local log_file="$LOG_DIR/style_fix_${proj}.log"

    # Capture base branch and write metadata for style usage logging
    local base_branch
    base_branch=$(git -C "$project_dir" branch --show-current)
    local meta_file="$RUN_DIR/style_meta_${proj}.txt"
    echo "base_branch=$base_branch" > "$meta_file"
    echo "project=$proj" >> "$meta_file"
    echo "timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$meta_file"

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
Run: bash ~/.claude/scripts/load-rust-style.sh --project-root $worktree_dir
Then read each unique style file referenced by the findings in EVALUATION.md. Each finding includes a **Style file** field with the full path to the style guide file (e.g., ~/rust/nate_style/rust/one-use-per-line.md or a repo-local docs/style/*.md file).

Step 2: Read the evaluation
Read the file: $worktree_dir/EVALUATION.md

Step 3: Apply ALL numbered findings from the evaluation.
Each evaluation run adds up to $MAX_NEW_FINDINGS new findings, but findings accumulate
across nightly runs via carry-forward. Apply every finding present.
- Read every file cited in the finding
- Apply the changes described in "Recommended pattern"
- Skip any finding whose cited files no longer exist or whose pattern no longer matches

Step 4: Run cargo mend and fix issues
Run: cargo mend --manifest-path $worktree_dir/Cargo.toml
- If mend fails due to missing Cargo.toml or missing toolchain, report the error and skip to Step 5.
- If mend reports fixable items, run: cargo mend --fix --manifest-path $worktree_dir/Cargo.toml
  - If mend --fix fails, report the error and skip to Step 5.
- If mend reports only unfixable items, note them and continue.

Step 5: Run clippy and fix any issues
Run: cargo clippy --workspace --all-targets --all-features --manifest-path $worktree_dir/Cargo.toml -- -D warnings
If clippy reports errors or warnings, fix them. Include any unfixable mend items from Step 4.

Step 6: Run tests and fix any failures
Run: cargo nextest run --workspace --manifest-path $worktree_dir/Cargo.toml
If any tests fail, fix them.

Step 7: Style review of the diff
Run: git -C $worktree_dir diff
If the diff is non-empty, evaluate every change against the style guide loaded in Step 1.
Fix any violations found. If no violations, move on.

Step 8: Write fix summary to EVALUATION.md
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

### Build Status
- **clippy:** pass | fail (with summary of remaining warnings/errors if fail)
- **tests:** pass | fail (with summary of failures if fail)

Fixing guidelines:
- Do NOT fix warnings by marking code as dead — remove dead code entirely
- Do NOT fix warnings by prefixing arguments/variables with _ — remove them if unused

Step 9: Log style usage.

Read the metadata file at $RUN_DIR/style_meta_${proj}.txt for base_branch, project, and timestamp values.
Each line is key=value format. Parse them to get the values.

For each unique style file referenced by findings in EVALUATION.md, append one JSON line
to $RUN_DIR/style_usage_${proj}.jsonl.

Use the Fix Summary you wrote in Step 8 to populate the findings array:
- "applied" for Status: Applied
- "partial" for Status: Partially applied (include reason from Issues field)
- "skipped" for Status: Skipped (include reason from Issues field)

Each JSON line must have these fields:
- timestamp: from the metadata file
- style_id: "shared:rust/<filename>" if the style file path is under ~/rust/nate_style/, or "local:<project>:<filename>" if under docs/style/
- style_file: just the filename (basename)
- local: true if under docs/style/, false if under ~/rust/nate_style/
- project: from the metadata file
- base_branch: from the metadata file
- findings: array of objects with finding (number), status ("applied"/"partial"/"skipped"), and reason (string, only for partial/skipped)

Keep skip/partial reasons to one sentence.
Write one valid JSON object per line, no trailing commas, no markdown fencing.

Rules:
- Modify ONLY files inside $worktree_dir (except the JSONL output to $RUN_DIR)
- Do NOT commit anything (no git add, no git commit)
- EVALUATION.md may ONLY be modified by appending the Fix Summary section (Step 8)
- Apply each fix completely — no partial changes
- If a finding references files that don't exist or patterns that don't match, skip that finding and document why in the Fix Summary
PROMPT_EOF
    )

    # Launch Claude to apply fixes
    if ! claude --print --dangerously-skip-permissions --settings '{"sandbox":{"enabled":false}}' -- "$prompt" > "$log_file" 2>&1; then
        echo "ERROR: $proj (claude fix application failed)"
        # Cleanup: move EVALUATION.md back before removing worktree
        [[ -f "$worktree_dir/EVALUATION.md" ]] && mv "$worktree_dir/EVALUATION.md" "$eval_file"
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

# Validate and merge per-project usage logs into the permanent log
if ls "$RUN_DIR"/style_usage_*.jsonl 1>/dev/null 2>&1; then
    echo "Merging style usage logs..."
    python3 -c "
import json, sys, pathlib
run_dir = pathlib.Path('$RUN_DIR')
merged = 0
rejected = 0
for f in sorted(run_dir.glob('style_usage_*.jsonl')):
    for i, line in enumerate(f.read_text().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            print(f'WARN: skipping malformed line {i} in {f.name}', file=sys.stderr)
            rejected += 1
        else:
            print(line)
            merged += 1
print(f'Merged {merged} entries ({rejected} rejected)', file=sys.stderr)
" >> ~/rust/nate_style/usage/log.jsonl
else
    echo "No style usage logs to merge."
fi

# Clean up per-run temp directory
rm -rf "$RUN_DIR"

# Update Obsidian summary
python3 ~/rust/nate_style/usage/summary.py --obsidian 2>&1 || {
    echo "WARNING: failed to update Obsidian summary"
}
