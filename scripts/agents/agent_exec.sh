#!/usr/bin/env bash

set -euo pipefail

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.11.
PY="${HOME}/.claude/scripts/lib/py"

agents_exec_print_argv() {
    local separator="" argument
    for argument in "$@"; do
        printf '%s' "$separator"
        printf '%q' "$argument"
        separator=" "
    done
}

# claude --print with --output-format stream-json writes one JSON event per
# line to the log as it happens (so heartbeat_watch.sh can narrate long turns);
# this extracts the final result event's text into the output file to keep the
# caller contract: output_file = final answer, log_file = full log.
agents_claude_extract_result() {
    "$PY" - "$1" "$2" "$3" <<'PY'
import json
import sys

log_path, out_path, task = sys.argv[1:]
result = None
errors = []
strict = task == "direct.simplify"
try:
    with open(log_path, encoding="utf-8", errors="replace") as log:
        for line in log:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                if isinstance(event.get("errors"), list):
                    errors.extend(reason for reason in event["errors"] if isinstance(reason, str) and reason.strip())
                if event.get("is_error"):
                    errors.append(event.get("result") or event.get("api_error") or event.get("subtype") or "Claude returned an error")
                if isinstance(event.get("result"), str):
                    result = event["result"]
except OSError:
    pass
if strict:
    if errors:
        result = "ERROR: " + "; ".join(dict.fromkeys(errors))
        print(result, file=sys.stderr)
with open(out_path, "w", encoding="utf-8") as out:
    out.write(result if result is not None else "")
if strict and errors:
    sys.exit(1)
PY
}

agents_exec_main() {
    if [[ "$#" -ne 6 ]]; then
        echo "Usage: agent_exec.sh <task> <write|readonly> <working_dir> <prompt_file> <output_file> <log_file>" >&2
        return 2
    fi

    local task="$1"
    local mode="$2"
    local working_dir="$3"
    local prompt_file="$4"
    local output_file="$5"
    local log_file="$6"
    local script_dir prompt family_args_line claude_code workers_home simplify_home selection_log claude_log_prefix=""
    local extraction_failure_is_error=false
    local -a family_args extra_args command task_args=() claude_env=()

    if [[ ! -f "$prompt_file" ]]; then
        printf 'Prompt not found: %s\n' "$prompt_file" > "$log_file"
        return 1
    fi

    case "$mode" in
        write|readonly) ;;
        *)
            echo "ERROR: mode must be 'write' or 'readonly'; got '$mode'." >&2
            return 2
            ;;
    esac

    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    source "$script_dir/agents_config.sh"
    agents_resolve "$task"
    if [[ "$task" == direct.simplify ]]; then
        [[ "$mode" == readonly ]] || return 2
        task_args=(--disallowedTools Edit,Write,NotebookEdit)
    fi

    prompt="$(cat "$prompt_file")"
    if [[ -n "${AGENT_EXEC_EXTRA_ARGS:-}" ]]; then
        # Whitespace-split with no quote interpretation; arguments cannot contain spaces, matching resolver emitters.
        read -r -a extra_args <<< "$AGENT_EXEC_EXTRA_ARGS"
    fi

    case "$AGENT_FAMILY" in
        codex)
            workers_home="$(agents_workers_home)" || return 1
            family_args_line="$(agents_codex_args)"
            read -r -a family_args <<< "$family_args_line"
            command=(env "CODEX_HOME=$workers_home" codex exec "${family_args[@]}")
            if [[ -n "${AGENT_EXEC_EXTRA_ARGS:-}" ]]; then
                command+=("${extra_args[@]}")
            fi
            command+=(--ephemeral)
            if [[ "$mode" == "write" ]]; then
                command+=(--sandbox danger-full-access)
            else
                command+=(--sandbox read-only)
            fi
            # The prompt goes in on stdin, never as an argument: a broad review
            # prompt carries the whole phase diff, and Linux caps one argv
            # string at 128 KiB, so an argument form dies with
            # "Argument list too long" (exit 126) before codex starts.
            command+=(-C "$working_dir" -o "$output_file" -)

            if [[ "${AGENT_EXEC_DRY_RUN:-}" == "1" ]]; then
                agents_exec_print_argv "${command[@]}"
                printf ' < '
                printf '%q' "$prompt_file"
                printf ' > '
                printf '%q' "$log_file"
                printf ' 2>&1\n'
                return 0
            fi
            "${command[@]}" < "$prompt_file" > "$log_file" 2>&1
            ;;
        claude)
            source "$script_dir/claude_account.sh"
            if [[ "$task" == direct.simplify ]]; then
                extraction_failure_is_error=true
                claude_env=(env CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1)
                selection_log="$log_file"
                [[ "${AGENT_EXEC_DRY_RUN:-}" != 1 ]] || selection_log=/dev/stderr
                if ! simplify_home="$(claude_launch_environment quota 2>"$selection_log")"; then
                    printf 'ERROR: %s\n' "${simplify_home:-unable to select a Claude account}" > "$log_file"
                    cat "$log_file" >&2
                    return 1
                fi
                export CLAUDE_CONFIG_DIR="$simplify_home"
                export CLAUDE_CODE_REMOTE_MEMORY_DIR="$HOME/.claude"
                unset CLAUDE_CODE_OAUTH_TOKEN
                if [[ "${AGENT_EXEC_DRY_RUN:-}" != 1 ]]; then
                    claude_log_prefix="$(cat "$log_file")"$'\n'
                    printf '%s' "$claude_log_prefix" >&2
                fi
            else
                claude_launch_environment workers || return 1
            fi
            family_args_line="$(agents_claude_args)"
            read -r -a family_args <<< "$family_args_line"
            command=("${claude_env[@]}" "$PY" "$script_dir/../lib/nix_env.py" exec "$(command -v claude)" --print)
            if [[ "$mode" == "write" ]]; then
                command+=(--dangerously-skip-permissions)
            else
                command+=(--permission-mode plan)
            fi
            command+=("${task_args[@]}")
            command+=(--settings '{"sandbox":{"enabled":false}}')
            command+=(--verbose --output-format stream-json)
            command+=("${family_args[@]}")
            if [[ -n "${AGENT_EXEC_EXTRA_ARGS:-}" ]]; then
                command+=("${extra_args[@]}")
            fi
            # The prompt goes in on stdin, never as an argument: a broad review
            # prompt carries the whole phase diff, and Linux caps one argv
            # string at 128 KiB, so an argument form dies with
            # "Argument list too long" (exit 126) before claude starts.

            if [[ "${AGENT_EXEC_DRY_RUN:-}" == "1" ]]; then
                printf 'cd '
                printf '%q' "$working_dir"
                printf ' && '
                agents_exec_print_argv "${command[@]}"
                printf ' < '
                printf '%q' "$prompt_file"
                printf ' > '
                printf '%q' "$log_file"
                printf ' 2>&1\n'
                return 0
            fi
            claude_code=0
            printf '%s' "$claude_log_prefix" > "$log_file"
            ( cd "$working_dir" && "${command[@]}" < "$prompt_file" >> "$log_file" 2>&1 ) || claude_code=$?
            if ! agents_claude_extract_result "$log_file" "$output_file" "$task"; then
                if "$extraction_failure_is_error"; then
                    claude_code=1
                fi
            fi
            return "$claude_code"
            ;;
        *)
            echo "ERROR: unsupported agent family '$AGENT_FAMILY'." >&2
            return 1
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    agents_exec_main "$@"
fi
