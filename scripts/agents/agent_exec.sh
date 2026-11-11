#!/usr/bin/env bash

set -euo pipefail

# python3 goes through the repo shim, which picks an interpreter by VERSION
# rather than by path: the python3 on PATH is Apple 3.9 on the Mac, and this
# repo needs >= 3.12.
PY="${BASH_SOURCE[0]%/*}/../lib/py"

agents_exec_print_argv() {
    local separator="" argument
    for argument in "$@"; do
        printf '%s' "$separator"
        printf '%q' "$argument"
        separator=" "
    done
}

# Every Claude print launch sets CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1.
# An is_error result or errors array gives ERROR: <reason> and exit 1.
# The output holds the final answer. The log holds the complete event stream.
agents_claude_extract_result() {
    "$PY" "$(dirname "${BASH_SOURCE[0]}")/claude_result.py" "$1" "$2"
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
    local script_dir prompt family_args_line claude_code workers_home extraction_code
    local -a family_args extra_args command task_args=()

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
    source "$script_dir/models_config.sh"
    models_launch_configuration || return 1
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
            ( models_agent_environment; "${command[@]}" < "$prompt_file" > "$log_file" 2>&1 )
            ;;
        claude)
            source "$script_dir/claude_account.sh"
            if [[ "$task" == direct.simplify ]]; then
                claude_launch_environment || return 1
            else
                claude_launch_environment workers || return 1
            fi
            AGENT_MODEL="$(_agents_model_slug claude "$AGENT_MODEL")" || return 1
            [[ "${AGENT_EXEC_DRY_RUN:-}" == 1 ]] || : > "$log_file"
            family_args_line="$(agents_claude_args)"
            read -r -a family_args <<< "$family_args_line"
            command=(env CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 "$PY" "$script_dir/../lib/nix_env.py" exec "$(command -v claude)" --print)
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
            ( cd "$working_dir" && models_agent_environment && "${command[@]}" < "$prompt_file" >> "$log_file" 2>&1 ) || claude_code=$?
            extraction_code=0
            agents_claude_extract_result "$log_file" "$output_file" || extraction_code=$?
            if [[ "$extraction_code" == 1 ]]; then
                claude_code=1
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
    "${BASH_SOURCE[0]%/*}/../lib/py" "${BASH_SOURCE[0]%/*}/../direct/assign_gate.py"
    agents_exec_main "$@"
fi
