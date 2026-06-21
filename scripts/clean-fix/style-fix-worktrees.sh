#!/bin/bash
# Create style-fix worktrees for targets with pending evaluation findings.
# For each eligible target: create a worktree, launch the configured style agent to apply fixes + clippy.
# Targets come from the [targets] allowlist in clean-fix.conf.
# Can be run standalone or called from clean-fix.sh.
#
# Usage: style-fix-worktrees.sh [project_name]
#   If project_name is given, only process that single target.
#   If omitted, process every eligible [targets] entry.
#
# Each [targets] line is either:
#   <dir>            a whole directory (single crate, or --workspace)
#   <dir>/<subpath>  one workspace member crate inside <dir>
# <dir> may be a primary repo or a worktree checkout (e.g. *_bevy_update).

set -euo pipefail

# Emit a final sentinel line on every exit path so a Monitor tailing the log
# can self-terminate (awk-on-match) and any /style_eval --fix orchestrator
# knows the script is gone, not just quiet. Format mirrors progress() so
# downstream consumers don't need a second regex.
__final_exit_code=0
__emit_launcher_exit() {
    __final_exit_code=$?
    local proj="${SINGLE_PROJECT:-all}"
    printf '[progress %s] phase=launcher-exit code=%s\n' "$proj" "$__final_exit_code"
}
trap __emit_launcher_exit EXIT

export PATH="$HOME/.local/bin:$PATH"
source "$HOME/.cargo/env"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/agent_assignments.sh"

RUST_DIR="$HOME/rust"
CONF_FILE="$SCRIPT_DIR/clean-fix.conf"
HISTORY_HELPER="$SCRIPT_DIR/style_history.py"
LOG_DIR="/private/tmp/claude"
SINGLE_PROJECT="${1:-}"
STYLE_ENABLED=""
STYLE_AGENT=""
STYLE_AGENT_MODEL=""
STYLE_AGENT_EFFORT=""
CODEX_BIN="${CODEX_BIN:-$(command -v codex 2>/dev/null || echo "$HOME/.local/bin/codex")}"

mkdir -p "$LOG_DIR"

# Stable phase markers a `Monitor` consumer (e.g. /style_eval --fix) can grep
# for line-by-line. Format: `[progress <project>] phase=<name> [k=v ...]`.
# Kept on its own line and on stdout so the manual launcher's log captures it.
progress() {
    local proj="$1"
    shift
    printf '[progress %s] %s\n' "$proj" "$*"
}

# Validate that $dir is a real git-linked worktree of $repo, not just a leftover
# directory. A directory with target/ but no .git linkage is
# an orphan stub from a partially-failed cleanup — treating it as a worktree
# turns a recoverable hiccup into a permanent self-skipping state.
is_real_worktree() {
    local dir="$1"
    local repo="$2"
    [[ -d "$dir" ]] || return 1
    git -C "$dir" rev-parse --git-dir >/dev/null 2>&1 || return 1
    git -C "$repo" worktree list --porcelain 2>/dev/null \
        | grep -qF "worktree $dir"
}

# Remove a worktree and verify it's gone. Tries `git worktree remove --force`
# first, falls back to `rm -rf`, then asserts the directory no longer exists.
# Returns 0 on success, 1 if the directory persists (a child process holding
# files open, permissions, etc.) — in which case the caller must fail loud so
# the next clean-fix doesn't silently skip on the leftover stub.
safe_remove_worktree() {
    local repo="$1"
    local dir="$2"
    git -C "$repo" worktree remove "$dir" --force 2>/dev/null || rm -rf "$dir"
    git -C "$repo" worktree prune 2>/dev/null || true
    if [[ -d "$dir" ]]; then
        rm -rf "$dir"
    fi
    [[ ! -d "$dir" ]]
}

# Diagnostic: change cwd to $RUST_DIR so any unanchored `cargo` call from this
# process or its children no longer references /Users/natemccoy/.claude. If the
# clean-fix log still shows that path tomorrow, the cargo invocation is coming
# from a process *outside* this script's tree (e.g. an IDE/LSP/watcher).
echo "[diag] style-fix-worktrees.sh starting: pid=$$ ppid=$PPID cwd_before=$(pwd)"
cd "$RUST_DIR"
echo "[diag] cwd_after_chdir=$(pwd)"

# Parse conf file for the [targets] allowlist and settings.
targets=()
MAX_NEW_FINDINGS=""
AGENT_TIMEOUT_SECS=""
POST_SUMMARY_GRACE_SECS=""
HEARTBEAT_INTERVAL_SECS=""

if [[ ! -f "$CONF_FILE" ]]; then
    echo "ERROR: conf file not found: $CONF_FILE" >&2
    echo "       [style_eval] max_new_findings must be set there." >&2
    exit 1
fi

if [[ -f "$CONF_FILE" ]]; then
    current_section=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="$(cf_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            current_section="${BASH_REMATCH[1]}"
            continue
        fi
        case "$current_section" in
            targets) targets+=("$stripped") ;;
            style_eval)
                if [[ "$stripped" =~ ^mode= ]]; then
                    echo "ERROR: [style_eval] mode is no longer supported; use enabled=true|false and agent=claude|codex" >&2
                    exit 1
                fi
                if [[ "$stripped" =~ ^max_new_findings=([0-9]+)$ ]]; then
                    MAX_NEW_FINDINGS="${BASH_REMATCH[1]}"
                fi
                ;;
            style_fix)
                if [[ "$stripped" =~ ^agent_timeout_secs=([0-9]+)$ ]]; then
                    AGENT_TIMEOUT_SECS="${BASH_REMATCH[1]}"
                elif [[ "$stripped" =~ ^post_summary_grace_secs=([0-9]+)$ ]]; then
                    POST_SUMMARY_GRACE_SECS="${BASH_REMATCH[1]}"
                elif [[ "$stripped" =~ ^heartbeat_interval_secs=([0-9]+)$ ]]; then
                    HEARTBEAT_INTERVAL_SECS="${BASH_REMATCH[1]}"
                elif [[ "$stripped" =~ ^mode= ]]; then
                    echo "ERROR: [style_fix] mode is no longer supported; agent settings moved to agent-assignments.conf" >&2
                    exit 1
                elif [[ "$stripped" =~ ^(enabled|agent|model|effort)= ]]; then
                    echo "ERROR: [style_fix] agent settings moved to $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE" >&2
                    exit 1
                fi
                ;;
        esac
    done < "$CONF_FILE"
fi

if [[ -z "$MAX_NEW_FINDINGS" ]]; then
    echo "ERROR: [style_eval] max_new_findings is not set in $CONF_FILE" >&2
    exit 1
fi
cf_load_stage_assignment style_fix STYLE_ENABLED STYLE_AGENT STYLE_AGENT_MODEL STYLE_AGENT_EFFORT || exit 1

# Default any [style_fix] tunables the user didn't set in the conf. Defaults
# match the values these were hard-coded to before the conf section existed.
: "${AGENT_TIMEOUT_SECS:=7200}"
: "${POST_SUMMARY_GRACE_SECS:=600}"
: "${HEARTBEAT_INTERVAL_SECS:=60}"

run_style_agent() {
    local project_root="$1"
    local prompt="$2"
    local log_file="$3"
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
            claude --print --dangerously-skip-permissions --settings '{"sandbox":{"enabled":false}}' \
                ${claude_args[@]+"${claude_args[@]}"} \
                -- "$final_prompt" > "$log_file" 2>&1
            ;;
        codex)
            final_prompt=$'IMPORTANT: Do NOT spawn sub-agents, delegate, or parallelize through helper agents. Complete this fix pass yourself in a single agent run.\nIMPORTANT: Do NOT create, replace, repair, or symlink the workspace path or any parent/peer repo path. If the expected workspace path is missing or invalid, fail and report it instead of trying to reconstruct it.\n\n'"$prompt"
            local codex_args=()
            if [[ -n "$STYLE_AGENT_MODEL" ]]; then
                codex_args+=("-m" "$STYLE_AGENT_MODEL")
            fi
            codex_args+=("-c" "model_reasoning_effort=\"${STYLE_AGENT_EFFORT:-xhigh}\"")
            "$CODEX_BIN" exec \
                ${codex_args[@]+"${codex_args[@]}"} \
                --ephemeral \
                --dangerously-bypass-approvals-and-sandbox \
                -C "$project_root" \
                -- "$final_prompt" \
                > "$log_file" 2>&1
            ;;
        *)
            echo "unsupported style_fix agent: $STYLE_AGENT" > "$log_file"
            return 1
            ;;
    esac
}

if [[ "$STYLE_ENABLED" == "false" ]]; then
    echo "Style fix is disabled."
    exit 0
fi

if [[ "$STYLE_AGENT" == "codex" && ! -x "$CODEX_BIN" ]]; then
    echo "ERROR: configured Codex binary is not executable: $CODEX_BIN" >&2
    exit 1
fi

# Eligibility pass. Parallel arrays:
#   eligible[] -- project names
#   records[]  -- fields joined by ASCII Unit Separator (0x1F):
#     name | kind | repo_dir | eval_file | worktree_dir | unused | subpath | pkg | branch
# Using 0x1F (not tab) so that empty fields in the middle (standalone projects
# have empty subpath and pkg) survive `read` without being collapsed by
# IFS whitespace-consolidation.
RS=$'\x1f'
eligible=()
records=()
skipped=0

# Resolve each [targets] entry:
#   <dir>            -> standalone: repo_dir = work_dir = ~/rust/<dir>, --workspace
#   <dir>/<subpath>  -> workspace_member: repo_dir = ~/rust/<dir>, work_dir = repo_dir/subpath
# <dir> may be a primary repo or a worktree checkout; `git rev-parse` accepts both.
for entry in ${targets[@]+"${targets[@]}"}; do
    if [[ "$entry" == */* ]]; then
        kind="workspace_member"
        subpath="${entry#*/}"
        pkg="${entry##*/}"
        name="$pkg"
        repo_dir="${RUST_DIR}/${entry%%/*}"
        work_dir="${RUST_DIR}/${entry}"
        worktree_dir="${RUST_DIR}/${name}_style_fix"
        unused=""
        branch_name="refactor/style/${name}"
    else
        kind="standalone"
        subpath=""
        pkg=""
        name="$entry"
        repo_dir="${RUST_DIR}/${entry}"
        work_dir="$repo_dir"
        worktree_dir="${RUST_DIR}/${name}_style_fix"
        unused=""
        branch_name="refactor/style"
    fi
    eval_file="$LOG_DIR/style_fix_${name}_evaluation.md"

    if [[ -n "$SINGLE_PROJECT" && "$name" != "$SINGLE_PROJECT" ]]; then
        continue
    fi

    # Rule: the work dir must exist and be a crate/workspace.
    if [[ ! -d "$work_dir" ]]; then
        echo "SKIP: $name (target path not found: $work_dir)"
        skipped=$((skipped + 1))
        continue
    fi
    if [[ ! -f "$work_dir/Cargo.toml" ]]; then
        echo "SKIP: $name (no Cargo.toml at $work_dir)"
        skipped=$((skipped + 1))
        continue
    fi

    # Rule: repo_dir must resolve to a git repo. `rev-parse` is true for a
    # primary checkout (.git dir) and a linked worktree (.git file) alike, so
    # worktree checkouts like *_bevy_update qualify without special-casing.
    if ! git -C "$repo_dir" rev-parse --git-dir >/dev/null 2>&1; then
        echo "SKIP: $name (not a git repo: $repo_dir)"
        skipped=$((skipped + 1))
        continue
    fi

    # Check 0: pending evaluation markdown exists with numbered findings.
    # No project-root evaluation markdown file is used as a sentinel anymore.
    eval_status=$(python3 "$HISTORY_HELPER" evaluation-status --project "$name" --field status 2>/dev/null || echo "missing")
    finding_count=$(python3 "$HISTORY_HELPER" evaluation-status --project "$name" --field finding_count 2>/dev/null || echo 0)
    coverage=$(python3 "$HISTORY_HELPER" evaluation-status --project "$name" --field coverage 2>/dev/null || echo "unknown")
    stop_reason=$(python3 "$HISTORY_HELPER" evaluation-status --project "$name" --field stop_reason 2>/dev/null || echo "unknown")
    [[ -z "$coverage" ]] && coverage="unknown"
    [[ -z "$stop_reason" ]] && stop_reason="in_progress"
    # Check A: The target _style_fix path must be free.
    if [[ -d "$worktree_dir" ]]; then
        # Distinguish a legitimate in-flight worktree (skip silently, expected)
        # from an orphan stub left by a partially-failed cleanup (surface loud).
        # An orphan looks like a worktree on disk but has no .git linkage and
        # isn't registered with the parent repo — every future clean-fix will
        # otherwise keep skipping the project until someone removes it by hand.
        if is_real_worktree "$worktree_dir" "$repo_dir"; then
            if [[ -f "$eval_file" ]] && rg -q '^## Fix Summary$' "$eval_file" 2>/dev/null; then
                finalized_work_dir="$worktree_dir"
                if [[ -n "$subpath" ]]; then
                    finalized_work_dir="$worktree_dir/$subpath"
                fi
                if python3 "$HISTORY_HELPER" finalize-fix --project-root "$finalized_work_dir" --evaluation "$eval_file"; then
                    echo "SKIP: $name (style_fix worktree already has Fix Summary; pending JSON updated)"
                else
                    echo "SKIP: $name (style_fix worktree has Fix Summary but pending update failed)"
                fi
            elif [[ "$eval_status" == "missing" ]]; then
                echo "SKIP: $name (style_fix worktree without pending JSON — manual state repair required)"
            else
                echo "SKIP: $name (style_fix worktree)"
            fi
        else
            echo "SKIP: $name (style_fix orphan stub — manual cleanup required at $worktree_dir)"
        fi
        skipped=$((skipped + 1))
        continue
    fi

    if [[ "$eval_status" == "fixed_findings" || "$eval_status" == "fix_failed_findings" ]]; then
        echo "SKIP: $name (pending $eval_status)"
        skipped=$((skipped + 1))
        continue
    fi
    if [[ "$eval_status" != "findings" && "$eval_status" != "reviewed_findings" ]]; then
        echo "SKIP: $name (no open findings)"
        skipped=$((skipped + 1))
        continue
    fi

    # If Git still has stale metadata for a missing style-fix worktree, prune it first.
    if git -C "$repo_dir" worktree list 2>/dev/null | grep -Fq "$worktree_dir"; then
        git -C "$repo_dir" worktree prune 2>/dev/null || true
        if git -C "$repo_dir" worktree list 2>/dev/null | grep -Fq "$worktree_dir"; then
            echo "SKIP: $name (style_fix worktree registered at target path)"
            skipped=$((skipped + 1))
            continue
        fi
    fi

    # (The old "no other worktree may exist" guard is gone: worktree checkouts
    # are now first-class targets, so the parent repo legitimately has several
    # worktrees. Check A above already guards the only slot that matters — the
    # <name>_style_fix path must be free.)

    # Check B: The working tree is clean.
    # For workspace_member, scope the dirty check to the member subpath.
    if [[ "$kind" == "workspace_member" ]]; then
        dirty=$(git -C "$repo_dir" status --porcelain -- "$subpath" 2>/dev/null || true)
    else
        dirty=$(git -C "$repo_dir" status --porcelain 2>/dev/null || true)
    fi
    if [[ -n "$dirty" ]]; then
        echo "SKIP: $name (working tree dirty)"
        skipped=$((skipped + 1))
        continue
    fi

    # Record eligibility. Delimited with 0x1F (unit separator) so empty middle
    # fields (subpath/pkg for standalone projects) survive `read` intact.
    record="${name}${RS}${kind}${RS}${repo_dir}${RS}${eval_file}${RS}${worktree_dir}${RS}${unused}${RS}${subpath}${RS}${pkg}${RS}${branch_name}"
    eligible+=("$name")
    records+=("$record")
    echo "ELIGIBLE: $name ($finding_count findings, coverage=$coverage, stop=$stop_reason, kind=$kind)"
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
# Sets globals: R_name R_kind R_repo_dir R_eval_file R_worktree_dir R_unused R_subpath R_pkg R_branch
load_record() {
    local target="$1"
    local i
    for i in "${!eligible[@]}"; do
        if [[ "${eligible[$i]}" == "$target" ]]; then
            IFS="$RS" read -r R_name R_kind R_repo_dir R_eval_file R_worktree_dir R_unused R_subpath R_pkg R_branch <<< "${records[$i]}"
            return 0
        fi
    done
    return 1
}

# Per-project environment from [project_env] in clean-fix.conf. Echoes the
# space-separated KEY=VALUE assignments for the given project, or nothing.
project_env_for() {
    local proj="$1" line section=""
    [[ -f "$CONF_FILE" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"; line="${line## }"; line="${line%% }"
        [[ -z "$line" ]] && continue
        if [[ "$line" =~ ^\[(.+)\]$ ]]; then section="${BASH_REMATCH[1]}"; continue; fi
        if [[ "$section" == "project_env" && "$line" == "$proj="* ]]; then
            echo "${line#*=}"; return 0
        fi
    done < "$CONF_FILE"
}

# Launch the configured style agent and supervise it until it exits, goes
# silent after producing its deliverable, or hits the hard timeout. Forwards
# the agent's `>>> phase:` markers to stdout as progress lines, emits a
# heartbeat every HEARTBEAT_INTERVAL_SECS, and once the deliverable sentinel
# appears SIGTERMs the agent after it has been silent for the grace window (so a
# verbose finishing agent is not cut off mid-write).
#
# Shared by the fix pass and the verify pass. It does NOT clean up the worktree
# on timeout — the caller decides, because the two passes treat a timeout
# differently (fix: discard the worktree; verify: keep the already-applied fix).
#
# Args: proj agent_work_dir prompt log_file sentinel_file sentinel_regex label detected_phase
# Sets: SUPERVISE_AGENT_CODE  (agent exit code; 143 if grace-SIGTERMed, 124 on hard timeout)
#       SUPERVISE_TIMED_OUT   (1 if hard-timeout-killed, else 0)
#       SUPERVISE_ELAPSED     (wall seconds the agent ran, in 10s steps)
supervise_agent() {
    local proj="$1"
    local agent_work_dir="$2"
    local prompt="$3"
    local log_file="$4"
    local sentinel_file="$5"
    local sentinel_regex="$6"
    local label="$7"
    local detected_phase="$8"

    SUPERVISE_AGENT_CODE=0
    SUPERVISE_TIMED_OUT=0
    SUPERVISE_ELAPSED=0

    # Phase-marker forwarding scans new bytes of the agent log inside the poll
    # loop below. We previously did this with a backgrounded
    # `( tail -F | while read ) &` subshell, but `kill $watcher_pid` only killed
    # the wrapping subshell — `tail -F` and the `while read` bash were reparented
    # to PID 1, kept inheriting our stdout (the pipe to tee in the parent
    # clean-fix script), and held the pipeline open for 26+ hours, blocking
    # launchd from firing the next night's run.
    : > "$log_file"  # ensure file exists so the offset math starts at 0

    echo "[diag $proj] launching $label agent ($STYLE_AGENT) in background"
    local agent_pid
    run_style_agent "$agent_work_dir" "$prompt" "$log_file" &
    agent_pid=$!
    echo "[diag $proj] $label agent launched: agent_pid=$agent_pid"
    progress "$proj" "phase=${label}-launch agent=$STYLE_AGENT pid=$agent_pid log=$log_file"

    local elapsed=0
    local timeout_secs="$AGENT_TIMEOUT_SECS"
    local summary_seen_at=""
    local post_summary_grace="$POST_SUMMARY_GRACE_SECS"
    local last_heartbeat=0
    local heartbeat_interval="$HEARTBEAT_INTERVAL_SECS"
    # Track agent log activity so we can defer SIGTERM while the agent is still
    # writing (e.g. expanding its deliverable section). The grace period is
    # measured from the LAST observed log activity, not the moment the sentinel
    # first appeared, so a verbose finishing phase does not get cut off.
    local last_log_size=0
    local last_activity_at=0
    [[ -f "$log_file" ]] && last_log_size=$(wc -c < "$log_file" 2>/dev/null || echo 0)
    while kill -0 "$agent_pid" 2>/dev/null; do
        sleep 10
        elapsed=$((elapsed + 10))
        if (( elapsed - last_heartbeat >= heartbeat_interval )); then
            progress "$proj" "phase=${label}-running elapsed=${elapsed}s"
            last_heartbeat=$elapsed
        fi
        local current_log_size=0
        [[ -f "$log_file" ]] && current_log_size=$(wc -c < "$log_file" 2>/dev/null || echo 0)
        if (( current_log_size > last_log_size )); then
            # Synchronously scan the new bytes for `>>> phase: <name>` markers
            # and republish each as a `progress` line on this script's stdout.
            # awk runs to completion in a foreground pipeline — no background
            # processes, no orphan risk.
            local delta=$((current_log_size - last_log_size))
            tail -c "$delta" "$log_file" 2>/dev/null \
                | awk -v proj="$proj" '
                    /^>>> phase: / {
                        sub(/^>>> phase: /, "")
                        printf "[progress %s] phase=agent-step %s\n", proj, $0
                        fflush()
                    }
                ' || true
            last_log_size=$current_log_size
            last_activity_at=$elapsed
        elif (( current_log_size < last_log_size )); then
            last_log_size=$current_log_size
        fi
        if [[ -z "$summary_seen_at" ]] && rg -q "$sentinel_regex" "$sentinel_file" 2>/dev/null; then
            summary_seen_at=$elapsed
            echo "[diag $proj] $label deliverable detected at ${elapsed}s; grace window resets on each agent log write"
            progress "$proj" "phase=$detected_phase elapsed=${elapsed}s"
        fi
        # Only SIGTERM after the deliverable sentinel is seen AND the agent has
        # been silent for the full grace window. Active agents resetting the
        # activity timer keep working past the original ceiling.
        if [[ -n "$summary_seen_at" ]] \
            && (( elapsed - summary_seen_at >= post_summary_grace )) \
            && (( elapsed - last_activity_at >= post_summary_grace )); then
            echo "[diag $proj] $label agent silent ${post_summary_grace}s after deliverable AND last log write at ${last_activity_at}s; sending SIGTERM"
            kill "$agent_pid" 2>/dev/null || true
            break
        fi
        if [[ $elapsed -ge $timeout_secs ]]; then
            kill "$agent_pid" 2>/dev/null || true
            sleep 5
            kill -9 "$agent_pid" 2>/dev/null || true
            wait "$agent_pid" 2>/dev/null || true
            SUPERVISE_AGENT_CODE=124
            SUPERVISE_TIMED_OUT=1
            SUPERVISE_ELAPSED=$elapsed
            return 0
        fi
    done

    # Tolerant cleanup: if any of these fail under `set -e`, the caller's
    # cleanup branches would never execute and the worktree would leak.
    wait "$agent_pid" && SUPERVISE_AGENT_CODE=0 || SUPERVISE_AGENT_CODE=$?
    # Final scan of any bytes the agent appended after the loop saw `kill -0`
    # fail (e.g. the very last write before exit).
    local final_log_size=0
    [[ -f "$log_file" ]] && final_log_size=$(wc -c < "$log_file" 2>/dev/null || echo 0)
    if (( final_log_size > last_log_size )); then
        local delta=$((final_log_size - last_log_size))
        tail -c "$delta" "$log_file" 2>/dev/null \
            | awk -v proj="$proj" '
                /^>>> phase: / {
                    sub(/^>>> phase: /, "")
                    printf "[progress %s] phase=agent-step %s\n", proj, $0
                    fflush()
                }
            ' || true
    fi
    SUPERVISE_ELAPSED=$elapsed
    progress "$proj" "phase=${label}-exit code=$SUPERVISE_AGENT_CODE elapsed=${elapsed}s"
    return 0
}

# Per-project function: create worktree and launch the configured style agent
create_and_fix() {
    local proj="$1"
    load_record "$proj" || { echo "ERROR: no record for $proj"; return 1; }
    local kind="$R_kind"
    local repo_dir="$R_repo_dir"
    local eval_file="$R_eval_file"
    local worktree_dir="$R_worktree_dir"
    # Keep evaluation markdown out of the worktree. Pending JSON owns durable
    # state; this scratch file is only the agent handoff + Fix Summary surface.
    local scratch_eval="$eval_file"
    local subpath="$R_subpath"
    local pkg="$R_pkg"
    local branch_name="$R_branch"
    local log_file="$LOG_DIR/style_fix_${proj}.log"

    # Per-project env (e.g. cargo-mend needs RUSTC_BOOTSTRAP=1 on stable). Exported
    # here so it reaches both the style-fix agent's cargo runs and the build-gate;
    # create_and_fix runs as its own backgrounded subshell, so this is scoped to
    # this project only.
    local proj_env
    proj_env=$(project_env_for "$proj")
    if [[ -n "$proj_env" ]]; then
        echo "[diag $proj] project_env: $proj_env"
        export $proj_env
    fi

    echo "[diag $proj] create_and_fix start: pid=$$ cwd=$(pwd)"
    progress "$proj" "phase=worktree-create kind=$kind"

    # Create worktree (-b fails if branch exists, which is intentional)
    echo "[diag $proj] before git worktree add"
    if ! git -C "$repo_dir" worktree add -b "$branch_name" "$worktree_dir" 2>>"$log_file"; then
        echo "ERROR: $proj (worktree creation failed — branch $branch_name may already exist)"
        progress "$proj" "phase=failed reason=worktree-create"
        return 1
    fi
    echo "[diag $proj] after git worktree add"

    # Apply canonical settings.local.json so manual review has permissions
    mkdir -p "$worktree_dir/.claude"
    cp "$HOME/.claude/templates/settings_local.json" "$worktree_dir/.claude/settings.local.json"
    echo "[diag $proj] after settings.local.json copy"

    # Materialize pending evaluation markdown only as a scratch handoff. Do not
    # write an evaluation markdown file into the style-fix worktree.
    mkdir -p "$(dirname "$scratch_eval")"
    rm -f "$scratch_eval"
    python3 "$HISTORY_HELPER" export-evaluation --project "$proj" --kind fix --output "$scratch_eval"
    echo "[diag $proj] after pending evaluation export"
    progress "$proj" "phase=worktree-ready dir=$worktree_dir"

    python3 "$HISTORY_HELPER" set-phase --project "$proj" --phase fix || true
    echo "[diag $proj] after set-phase (about to build prompt + launch agent)"

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

    # Build prompt for the agent.
    # Write to a temp file then slurp it back. Do NOT use `$(cat <<EOF ... EOF)`
    # — macOS bash 3.2 misparses backticks and apostrophes in the heredoc body
    # when the heredoc is inside command substitution, emitting spurious
    # "command not found" errors at runtime.
    local prompt prompt_file
    prompt_file="$LOG_DIR/style_fix_prompt_${proj}_$$.txt"
    cat > "$prompt_file" <<PROMPT_EOF
You are applying automated style fixes to a Rust project.
Working directory: $agent_work_dir
Worktree root: $worktree_dir

IMPORTANT: Do NOT spawn sub-agents, delegate, or parallelize through helper agents. Complete this fix pass yourself in a single agent run.

IMPORTANT: Run every lint command through \`lint\` in the FOREGROUND and read its output directly. Do NOT run cargo/lint in the background and poll for it to finish with \`pgrep -f "cargo ..."\` or \`pgrep -f "lint ..."\` — those substrings can match this very fix prompt (your own claude process argv contains these command names) and the polling command itself, so \`! pgrep ...\` is never true and the wait loop spins forever.

**Progress markers (REQUIRED — emit before doing each step).** Print one line of the exact form below on stdout immediately before you begin each named phase. The orchestrator's log watcher reads these lines and forwards them to the user's chat as live progress; without them, the user sees only a 60s heartbeat and has no idea which phase you're in.

    >>> phase: <name> [extra=value …]

Required phase names, in order:

- \`>>> phase: read-style-guide\` — before Step 1 (load style guide).
- \`>>> phase: read-evaluation\` — before Step 2 (read the scratch evaluation file).
- \`>>> phase: apply-finding n=<N> id=<short-id>\` — once per finding before its first edit (\`<short-id>\` = the guideline filename stem from the **Style file** field, e.g. \`enums-over-bool-for-owned-booleans\`).
- \`>>> phase: cargo-mend-preview\` — before \`lint mend ... --manifest-path\` in Step 4.
- \`>>> phase: cargo-mend-fix\` — before \`lint mend --fix ... --manifest-path\` (skip if no fixable items).
- \`>>> phase: clippy-preview\` — before Step 5a.
- \`>>> phase: clippy-auto-fix\` — before Step 5b (skip if 5a was clean).
- \`>>> phase: clippy-manual\` — before Step 5c.
- \`>>> phase: tests\` — before Step 6.
- \`>>> phase: style-review\` — before Step 7.
- \`>>> phase: fmt\` — before Step 8.
- \`>>> phase: write-fix-summary\` — before Step 9.

Emit each marker on its own line, with no other text on the line. Do not skip markers even when a step has nothing to do — emit the marker and proceed (the user wants to see the step ran).

Step 1: Load the style guide and read referenced files
Run: zsh ~/.claude/scripts/rust_style/load-rust-style.sh --project-root $agent_work_dir
Then read each unique style file referenced by the findings in the scratch evaluation file. Each finding includes a **Style file** field with the full path to the style guide file (e.g., ~/rust/nate_style/rust/one-use-per-line.md or a repo-local docs/style/*.md file).
Also read each style file marked [non-negotiable] in the loaded checklist, even if no finding cites it directly. Those rules apply to every fix.

Step 2: Read the evaluation
Read the scratch evaluation file: $scratch_eval

IMPORTANT — review-stage exclusions:
- Any finding wrapped in \`<!-- REMOVED-BY-REVIEW: ... -->\` ... \`<!-- /REMOVED-BY-REVIEW -->\` markers has been struck by the review pass. Treat it as if absent. Do NOT apply it. Do NOT mention it in the Fix Summary except to note it was removed-by-review.
- The \`## Review Log\` section at the bottom of the scratch evaluation file is reporting-only metadata for the human reviewer. Do NOT act on anything it says. Do NOT modify it.
- Apply only the numbered findings whose body is NOT inside REMOVED-BY-REVIEW markers.

Step 3: Apply numbered findings from the evaluation.
Each evaluation run adds up to $MAX_NEW_FINDINGS new findings, but findings accumulate
across clean-fix runs via carry-forward. Process every finding present, but how you process
it depends on the governing guideline frontmatter \`mode:\` field.

**For each finding, before touching code:** open the guideline file from the
**Style file** field of the finding and read its frontmatter \`mode:\` value.

- If \`mode: propose\` (or any \`mode:\` other than the listed apply-modes below):
  - Do **NOT** modify any code for this finding.
  - In the Fix Summary, set Status to **Proposed** (not Applied or Skipped).
  - Write a "What I would change" paragraph describing the recommended edits site-by-site
    using the Locations list, plus a "Why" paragraph naming the tradeoff the user needs to
    weigh in on. The user reviews and decides during \`/style_fix_review\`.

- If \`mode: auto\`, \`mode: flag\`, or no \`mode:\` field (default for \`mechanism: llm\`):
  - Read every file in the **Locations** list of the finding.
  - Before editing, rerun the finding's **Search** command when it is safe and still applies in this worktree. If the command is stale, derive an equivalent project-wide search from the **Surface searched** line and the style rule. Any still-valid same-rule matches you find are part of this finding's work set, even if the eval missed them.
  - **Before renaming a symbol or changing a public signature, use LSP \`findReferences\`**
    on the target to enumerate every call site you will need to update. ripgrep misses
    references that go through type aliases, re-exports, or generic dispatch; LSP does not.
    Apply the rename and update every reference returned. Same applies to relocations
    and visibility narrowing — \`findReferences\` first, then edit.
  - Apply the "Recommended pattern" at **every listed location and every same-rule
    match found by the pre-fix search**. Do not stop after the listed examples if
    the project-wide search shows more matches for this same finding.
  - Skip any individual location whose file no longer exists or whose pattern no longer
    matches; if a finding has zero matching locations remaining, skip it and document why
    in the Fix Summary.
  - If applying a finding as written would violate any [non-negotiable] rule, do NOT apply
    that conflicting change. Preserve the non-negotiable rule, make any safe partial
    progress you can, and document the conflict in the Fix Summary.
  - After editing, rerun the same project-wide search. If it reports any remaining
    violation for this finding, set Status to **Partially applied** and list the
    remaining sites in **Post-fix search** or **Issues**. Only use **Applied** when
    the post-fix search says \`0 remaining\`.

LSP availability: claude has the \`LSP\` tool when \`ENABLE_LSP_TOOL=1\` is in env;
codex has equivalent coverage via the \`mcp-language-server\` MCP server. If neither
is reachable, fall back to ripgrep but expand the scope (search the whole crate, not
just the cited file) and document the limitation in the Fix Summary.

**Do NOT delete or rewrite existing documentation as a side effect of any fix.**
Each recommended pattern in a finding is a structural code change (split a module,
bundle parameters, rename a binding, switch to a \`From\` impl). None of those
patterns require touching comments or doc strings. Specifically:

- **Preserve** all \`///\` doc comments on items, fields, and uniform/\`ShaderType\`
  struct fields — these document the GPU contract or public API and live nowhere else.
- **Preserve** inline \`//\` comments that explain coordinate-space conversions,
  shader semantics, or other non-obvious WHYs.
- **Update** only the comments your edit makes literally inaccurate (e.g. a
  parameter name you just renamed). Update; do not delete.
- The "default to no comments" guidance in the global instructions applies to
  *writing new code*. It does NOT authorize pruning existing documentation.

If you believe a comment is genuinely stale (describes code that no longer
exists), leave it and note it in the Fix Summary as a comment-only follow-up.
The user reviews comment changes during \`/style_fix_review\`.

Step 4: Run cargo mend through the shared lint wrapper and fix issues
Run: lint mend --manifest-path $worktree_dir/Cargo.toml
- If mend fails due to missing Cargo.toml or missing toolchain, report the error and skip to Step 5.
- If mend reports fixable items, run: lint mend --fix --manifest-path $worktree_dir/Cargo.toml
  - If mend --fix fails, report the error and skip to Step 5.
- If mend reports only unfixable items, note them and continue.

Step 5: Run clippy and fix any issues
Step 5a (preview): Run: lint clippy --manifest-path $worktree_dir/Cargo.toml
- Capture the list of warnings/errors reported. This is the baseline of what clippy sees.
- If clippy reports nothing, skip to Step 6.
- If clippy fails because the toolchain itself is missing, report the error and skip to Step 6.
- If clippy fails with a COMPILE error, the tree is broken — almost always because an earlier step (a module split, rename, extraction, or move) left a dangling reference or missing import. STOP. Do NOT proceed to Step 5b, 5c, 6, or beyond until the tree compiles. Fix the cause directly (restore the missing import, re-add the dropped item, repoint the reference). If the breakage came from a finding you applied and you cannot fix it in place, revert that finding's edits and mark the finding \`Skipped\` in the Fix Summary with the compile error as the reason. Compile errors are NEVER "infrastructure" — they are caused by the edits in this run.

Step 5b (auto-fix): If Step 5a reported any fixable items, run: lint clippy --fix --allow-dirty --manifest-path $worktree_dir/Cargo.toml
- This auto-applies every fix clippy can make on its own. Do NOT manually fix anything clippy could have auto-fixed.
- If --fix fails, report the error and fall through to Step 5c to handle remaining items manually.

Step 5c (verify + manual): Re-run: lint clippy --manifest-path $worktree_dir/Cargo.toml
- Anything still reported after 5b is either unfixable by clippy or a fix that conflicts with style. Manually fix those now.
- Include any unfixable mend items from Step 4 in this manual pass.
- After fixing, re-run clippy one more time to confirm clean; only spend evaluation effort on items that actually remain.

Step 6: Run tests and fix any failures
Run: CARGO_MEND_SKIP_NETWORK_TESTS=1 cargo nextest run $cargo_scope_flag --manifest-path $worktree_dir/Cargo.toml
The CARGO_MEND_SKIP_NETWORK_TESTS env var asks tests that require localhost TCP (e.g. nested \`cargo fix\` invocations) to skip themselves; the sandbox blocks those binds with \`Operation not permitted (os error 1)\` and they cannot succeed here.
If any non-skipped tests fail, fix them.

Step 7: Style review of the diff
Run: git -C $worktree_dir diff | grep '^+' | grep -v '^+++' > /tmp/claude/style-review-additions.txt
If the file is empty, skip to Step 8 (fmt).

Find the === STYLE_CHECKLIST === section from the style guide output in Step 1.
For each rule in the checklist, check the additions-only diff for violations.
Fix any violations found. If no violations, move on.
For rules marked [non-negotiable], review the full diff intent, not just added lines. Reversions, deletions, or signature changes that violate a non-negotiable rule must be fixed or the conflicting finding must be marked partial/skipped with an explanation.

Step 8: Run cargo +clean-fix fmt
Run: cargo +clean-fix fmt $cargo_scope_flag --manifest-path $worktree_dir/Cargo.toml

Step 9: Write fix summary to the scratch evaluation file
Append a section to the END of $scratch_eval with the following format:

---

## Fix Summary

For each numbered finding, add a line:

### Finding N: [title from finding]
**Status:** Applied | Partially applied | Skipped | Proposed
**What was done:** [1-2 sentences describing the actual changes made]
**Post-fix search:** [exact command or equivalent search used] — [0 remaining | N remaining: `path:line`, ...]
**What I would change** (Proposed only): [paragraph describing recommended edits per Location]
**Why** (Proposed only): [paragraph naming the tradeoff for the user to weigh in on]
**Issues:** [If partially applied or skipped, explain WHY — e.g., "file no longer exists",
"pattern did not match", "fixing this would require removing a public API method",
"the clippy-suggested fix conflicts with one-use-per-line style rule", etc.]
[Omit Issues line if status is Applied with no complications, but do not omit Post-fix search for Applied findings]
[Use Proposed when the guideline frontmatter has \`mode: propose\` — no code changes were made]

After all findings, add:

### Cargo Mend Changes
If lint mend --fix was run in Step 4 and made any changes, summarize them here:
- List the files modified by lint mend
- Describe the types of changes (e.g., "narrowed pub to pub(crate)", "shortened import paths")
- If lint mend was skipped or found nothing to fix, say so explicitly

### Clippy Changes
Summarize Step 5:
- **Preview (5a):** count and types of warnings/errors clippy reported, or "clean"
- **Auto-fix (5b):** files modified by \`lint clippy --fix\` and the categories of fixes applied, or "not run" if 5a was clean
- **Manual (5c):** anything that remained after --fix and had to be hand-fixed (or that was left unfixed because the suggested fix conflicts with a style rule — explain)

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
- The scratch evaluation file ($scratch_eval) may ONLY be modified by appending the Fix Summary section (Step 9). Do NOT edit findings, REMOVED-BY-REVIEW blocks, or the Review Log. Append the Fix Summary AFTER the Review Log if one is present. Do NOT create an evaluation markdown file in the worktree.
- Apply each fix completely — no partial changes
- If a finding references files that do not exist or patterns that do not match, skip that finding and document why in the Fix Summary
PROMPT_EOF
    prompt=$(<"$prompt_file")
    rm -f "$prompt_file"

    # Pass 1 (apply): launch the configured style agent to apply the findings.
    # supervise_agent watches it, forwards its `>>> phase:` markers, and bounds
    # it by the grace + hard-timeout rules. Deliverable sentinel = Fix Summary.
    supervise_agent "$proj" "$agent_work_dir" "$prompt" "$log_file" \
        "$scratch_eval" '^## Fix Summary$' "agent" "agent-fix-summary-detected"
    local agent_code=$SUPERVISE_AGENT_CODE

    if [[ "$SUPERVISE_TIMED_OUT" == "1" ]]; then
        echo "TIMEOUT: $proj ($STYLE_AGENT exceeded ${AGENT_TIMEOUT_SECS}s timeout)"
        progress "$proj" "phase=failed reason=timeout elapsed=${SUPERVISE_ELAPSED}s"
        python3 "$HISTORY_HELPER" finalize-failure --project "$proj" --reason "$STYLE_AGENT exceeded ${AGENT_TIMEOUT_SECS}s timeout" || true
        if ! safe_remove_worktree "$repo_dir" "$worktree_dir"; then
            echo "ERROR: $proj (worktree directory persists at $worktree_dir after cleanup — manual intervention needed)"
            progress "$proj" "phase=failed reason=cleanup-leftover dir=$worktree_dir"
        fi
        if [[ "$(git -C "$repo_dir" rev-list --count "main..$branch_name" 2>/dev/null)" == "0" ]]; then
            git -C "$repo_dir" branch -D "$branch_name" 2>/dev/null || true
        else
            echo "WARN: $proj (kept $branch_name branch — has unmerged commits)"
        fi
        return 1
    fi

    if [[ $agent_code -ne 0 ]]; then
        if [[ -f "$scratch_eval" ]] && rg -q '^## Fix Summary$' "$scratch_eval"; then
            echo "WARN: $proj ($STYLE_AGENT exited $agent_code, but Fix Summary was produced)"
        else
            local fail_reason
            if [[ ! -s "$log_file" ]]; then
                fail_reason="$STYLE_AGENT exited immediately with no output"
            else
                fail_reason="$STYLE_AGENT failed after producing output (see $log_file)"
            fi
            echo "ERROR: $proj ($fail_reason)"
            progress "$proj" "phase=failed reason=agent-exit code=$agent_code"
            python3 "$HISTORY_HELPER" finalize-failure --project "$proj" --reason "$fail_reason" || true
            if ! safe_remove_worktree "$repo_dir" "$worktree_dir"; then
                echo "ERROR: $proj (worktree directory persists at $worktree_dir after cleanup — manual intervention needed)"
                progress "$proj" "phase=failed reason=cleanup-leftover dir=$worktree_dir"
            fi
            if [[ "$(git -C "$repo_dir" rev-list --count "main..$branch_name" 2>/dev/null)" == "0" ]]; then
                git -C "$repo_dir" branch -D "$branch_name" 2>/dev/null || true
            else
                echo "WARN: $proj (kept $branch_name branch — has unmerged commits)"
            fi
            return 1
        fi
    fi

    # Pass 2 (verify): an independent run of the SAME configured agent
    # (STYLE_AGENT/model/effort) checks the applied fix against the findings and
    # the Fix Summary, corrects any incomplete or wrong fixes itself, and updates
    # the Fix Summary. Deliverable sentinel = a `## Fix Verification` section.
    # Only reached when a Fix Summary exists. A verify miss (timeout / no
    # section) is non-fatal: the applied fix is already reviewable, so we keep
    # the worktree and let the build gate below have the final say. Verify status
    # is reported only via progress/diag lines — never `WARN:/ERROR: <proj>`,
    # which the report parser would mis-read as a fix-phase failure.
    progress "$proj" "phase=verify-start"
    local verify_log="$LOG_DIR/style_fix_verify_${proj}.log"
    local verify_prompt verify_prompt_file
    verify_prompt_file="$LOG_DIR/style_fix_verify_prompt_${proj}_$$.txt"
    cat > "$verify_prompt_file" <<VERIFY_EOF
You are independently verifying automated style fixes that another agent just
applied to a Rust project. Confirm every fix is correct and complete, correct
any mistakes yourself, and update the Fix Summary to reflect the true state.

Working directory: $agent_work_dir
Worktree root: $worktree_dir

IMPORTANT: Do NOT spawn sub-agents, delegate, or parallelize through helper agents. Complete this verification yourself in a single agent run.

IMPORTANT: Run every lint command (clippy, fmt) and every test command in the FOREGROUND and read its output directly. Do NOT run cargo/lint in the background and poll for it with \`pgrep\` — that substring can match your own process argv and the wait loop spins forever.

**Progress markers (REQUIRED — emit before doing each step).** Print one line of the exact form below on stdout immediately before you begin each named phase. The orchestrator forwards these to the user's chat as live progress.

    >>> phase: <name> [extra=value …]

Required phase names, in order:

- \`>>> phase: verify-read-style-guide\` — before Step 1.
- \`>>> phase: verify-read-evaluation\` — before Step 2.
- \`>>> phase: verify-inspect-diff\` — before Step 3.
- \`>>> phase: verify-finding n=<N> id=<short-id>\` — once per finding before you re-check it (\`<short-id>\` = the guideline filename stem from the **Style file** field).
- \`>>> phase: verify-clippy\` — before re-running clippy (only if you changed code).
- \`>>> phase: verify-tests\` — before re-running tests (only if you changed code).
- \`>>> phase: verify-fmt\` — before fmt (only if you changed code).
- \`>>> phase: write-verification\` — before Step 6.

Step 1: Load the style guide
Run: zsh ~/.claude/scripts/rust_style/load-rust-style.sh --project-root $agent_work_dir
Read each unique style file referenced by the findings (the **Style file** field of each), plus every style file marked [non-negotiable] in the loaded checklist.

Step 2: Read the evaluation and the Fix Summary
Read: $scratch_eval
It contains, in order:
- \`## Improvements\` — the numbered findings the fix agent was told to apply. Any finding wrapped in \`<!-- REMOVED-BY-REVIEW ... -->\` markers was struck before the fix and must remain UNAPPLIED.
- \`## Review Log\` (if present) — reporting-only. Do not act on it; do not modify it.
- \`## Fix Summary\` — the fix agent's per-finding claims (Status, What was done, Post-fix search, Issues).

Step 3: Inspect what was actually changed
Run: git -C $worktree_dir diff
Read the full diff — this is the complete set of edits the fix agent made.

Step 4: Verify each finding against the Fix Summary, the diff, and the code
For each numbered finding that is NOT inside REMOVED-BY-REVIEW markers, in order:
1. Read its governing **Style file** and re-read the \`mode:\` frontmatter.
2. Re-run the finding's post-fix search (the **Post-fix search** or **Search** command, or an equivalent project-wide search derived from **Surface searched**) against the current worktree.
3. Decide whether the Fix Summary's claim is actually TRUE:
   - **Applied / 0 remaining**: confirm the search really returns 0 matches AND the diff implements the recommended pattern at every listed location. If any matching site remains, the fix is INCOMPLETE — apply the missing edits now. Before renaming a symbol or changing a public signature, use LSP \`findReferences\` to enumerate call sites; ripgrep misses references through aliases, re-exports, and generic dispatch.
   - **Partially applied / Skipped**: confirm the stated reason is legitimate (file gone, pattern absent, non-negotiable conflict). If the work was actually doable and the reason is wrong, complete it now.
   - **Proposed** (guideline \`mode: propose\`): confirm NO code was changed for it — these are for the human to decide. If the fix agent applied code for a propose-mode finding, that is a mistake: revert those edits.
4. Confirm no non-removed finding was silently dropped (each has a Fix Summary entry).
5. Confirm the diff introduced no NEW violation of any [non-negotiable] rule; fix any it introduced.

You ARE authorized to edit code in the worktree to correct incomplete, incorrect, or non-negotiable-violating fixes. Do NOT add fixes for unrelated rules the original findings did not cover — that is the next /style_eval's job, not this pass. Preserve all existing \`///\` doc comments and explanatory \`//\` comments.

Step 5: Re-verify the build (only if you changed any code in Step 4)
- Run: lint clippy --manifest-path $worktree_dir/Cargo.toml
  A compile error means an edit left a dangling reference — fix it before continuing.
- Run: CARGO_MEND_SKIP_NETWORK_TESTS=1 cargo nextest run $cargo_scope_flag --manifest-path $worktree_dir/Cargo.toml
  Fix any failures you introduced.
- Run: cargo +clean-fix fmt $cargo_scope_flag --manifest-path $worktree_dir/Cargo.toml
If you changed no code, skip this step.

Step 6: Update the Fix Summary, then append the Fix Verification section
First, update the existing \`## Fix Summary\` in place so each finding's **Status**, **What was done**, and **Post-fix search** lines state the TRUE post-verification result. Do not renumber. Do not touch the \`## Improvements\` findings or the \`## Review Log\`.

Then append this section to the END of $scratch_eval:

---

## Fix Verification

**Verified:** [YYYY-MM-DD]
**Findings verified:** [N]

### Per-finding verdict
For each numbered (non-removed) finding, one bullet:
- **Finding N** — confirmed | corrected | completed | downgraded | proposed-left-as-is — [one-line note: what you checked and what, if anything, you changed]

### Corrections made
[One bullet per code edit you made to correct the fix, by file. Write "None — all fixes confirmed correct and complete." if you changed nothing.]

### Build status after verification
- **clippy:** pass | fail | not-re-run (no code changed)
- **tests:** pass | fail | not-re-run (no code changed)

Rules:
- $review_dir_description
- Do NOT commit anything (no git add, no git commit). Do NOT create or switch branches.
- Modify $scratch_eval only by (a) updating \`## Fix Summary\` status lines to reflect your corrections and (b) appending the \`## Fix Verification\` section. Do NOT edit the \`## Improvements\` findings, the REMOVED-BY-REVIEW blocks, or the \`## Review Log\`.
- Single agent run; foreground cargo only.
VERIFY_EOF
    verify_prompt=$(<"$verify_prompt_file")
    rm -f "$verify_prompt_file"

    supervise_agent "$proj" "$agent_work_dir" "$verify_prompt" "$verify_log" \
        "$scratch_eval" '^## Fix Verification$' "verify" "verify-summary-detected"

    if rg -q '^## Fix Verification$' "$scratch_eval" 2>/dev/null; then
        echo "[diag $proj] verify pass complete (Fix Verification section present)"
        progress "$proj" "phase=verify-done elapsed=${SUPERVISE_ELAPSED}s"
    elif [[ "$SUPERVISE_TIMED_OUT" == "1" ]]; then
        echo "[diag $proj] verify pass timed out after ${AGENT_TIMEOUT_SECS}s; keeping applied fix for review"
        progress "$proj" "phase=verify-incomplete reason=timeout elapsed=${SUPERVISE_ELAPSED}s"
    else
        echo "[diag $proj] verify pass exited ${SUPERVISE_AGENT_CODE} with no Fix Verification section; keeping applied fix for review"
        progress "$proj" "phase=verify-incomplete reason=agent-exit code=${SUPERVISE_AGENT_CODE}"
    fi

    # Build gate: the agent self-reports build status in its Fix Summary, but
    # the field is free text and the runner has historically trusted it. Re-run
    # cargo check from outside the agent so a broken tree cannot be declared
    # successful. Matches the scope the agent itself uses for clippy.
    local build_check_log="$RUN_DIR/$proj.build-check.log"
    progress "$proj" "phase=build-gate log=$build_check_log"
    if ! cargo check $cargo_scope_flag --all-targets --all-features \
            --manifest-path "$worktree_dir/Cargo.toml" \
            >"$build_check_log" 2>&1; then
        echo "FAIL: $proj (cargo check failed after style-fix; worktree left for review at $worktree_dir; see $build_check_log)"
        progress "$proj" "phase=failed reason=build-broken-after-fix log=$build_check_log"
        if [[ -f "$scratch_eval" ]]; then
            python3 "$HISTORY_HELPER" save-evaluation \
                --project-root "$agent_work_dir" \
                --evaluation "$scratch_eval" || true
        fi
        python3 "$HISTORY_HELPER" finalize-failure --project "$proj" \
            --reason "cargo check failed after style-fix; worktree left at $worktree_dir for review" || true
        # Intentionally do NOT remove the worktree or branch here. The agent
        # finished writing its Fix Summary, the diff is reviewable, and the
        # user needs to see what broke. The failure paths elsewhere in this
        # script clean up because the agent never produced reviewable output;
        # this path is the opposite case.
        return 1
    fi

    python3 "$HISTORY_HELPER" finalize-fix --project-root "$agent_work_dir" --evaluation "$scratch_eval" || {
        echo "ERROR: $proj (could not finalize history)"
        progress "$proj" "phase=failed reason=finalize-history"
        return 1
    }

    echo "OK: $proj (worktree created, fixes applied)"
    progress "$proj" "phase=done eval=$scratch_eval"
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
        if load_record "$proj"; then
            # If the first attempt already produced a scratch evaluation with
            # a Fix Summary, the run is effectively done. The
            # "failure" was almost certainly a SIGTERM-on-grace race or a
            # tolerant-cleanup gap. Finalize history and treat as success
            # instead of recreating the worktree.
            #
            # Require a real git-linked worktree, not just a directory. The
            # scratch evaluation proves the agent reached the summary phase;
            # the worktree check proves the diff belongs to a real worktree.
            if is_real_worktree "$R_worktree_dir" "$R_repo_dir" \
                && [[ -f "$R_eval_file" ]] \
                && rg -q '^## Fix Summary$' "$R_eval_file" 2>/dev/null; then
                echo "RETRY-SKIP: $proj (scratch evaluation already has Fix Summary; finalizing without re-running agent)"
                progress "$proj" "phase=already-applied dir=$R_worktree_dir"
                already_work_dir="$R_worktree_dir"
                if [[ -n "$R_subpath" ]]; then
                    already_work_dir="$R_worktree_dir/$R_subpath"
                fi
                if python3 "$HISTORY_HELPER" finalize-fix --project-root "$already_work_dir" --evaluation "$R_eval_file"; then
                    echo "RETRY OK: $proj (already applied)"
                    progress "$proj" "phase=done eval=$R_eval_file"
                    failed=$((failed - 1))
                    succeeded=$((succeeded + 1))
                    continue
                else
                    echo "RETRY FAILED: $proj (finalize-history error)"
                    progress "$proj" "phase=failed reason=finalize-history"
                    continue
                fi
            fi

            # Worktree did NOT carry a finished fix; clean up before recreating.
            git -C "$R_repo_dir" branch -D "$R_branch" 2>/dev/null || true
            if [[ -d "$R_worktree_dir" ]]; then
                if ! safe_remove_worktree "$R_repo_dir" "$R_worktree_dir"; then
                    echo "ERROR: $proj (worktree directory persists at $R_worktree_dir before retry — skipping retry)"
                    progress "$proj" "phase=failed reason=cleanup-leftover dir=$R_worktree_dir"
                    continue
                fi
            fi
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
