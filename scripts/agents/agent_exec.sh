#!/usr/bin/env bash

set -euo pipefail

agents_exec_print_argv() {
    local separator="" argument
    for argument in "$@"; do
        printf '%s' "$separator"
        printf '%q' "$argument"
        separator=" "
    done
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
    local script_dir prompt family_args_line
    local -a family_args extra_args command

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

    prompt="$(cat "$prompt_file")"
    if [[ -n "${AGENT_EXEC_EXTRA_ARGS:-}" ]]; then
        # Whitespace-split with no quote interpretation; arguments cannot contain spaces, matching resolver emitters.
        read -r -a extra_args <<< "$AGENT_EXEC_EXTRA_ARGS"
    fi

    case "$AGENT_FAMILY" in
        codex)
            family_args_line="$(agents_codex_args)"
            read -r -a family_args <<< "$family_args_line"
            command=(codex exec "${family_args[@]}")
            if [[ -n "${AGENT_EXEC_EXTRA_ARGS:-}" ]]; then
                command+=("${extra_args[@]}")
            fi
            command+=(--ephemeral)
            if [[ "$mode" == "write" ]]; then
                command+=(--full-auto)
            else
                command+=(--sandbox read-only)
            fi
            command+=(-C "$working_dir" -o "$output_file" "$prompt")

            if [[ "${AGENT_EXEC_DRY_RUN:-}" == "1" ]]; then
                agents_exec_print_argv "${command[@]}"
                printf ' > '
                printf '%q' "$log_file"
                printf ' 2>&1\n'
                return 0
            fi
            "${command[@]}" > "$log_file" 2>&1
            ;;
        claude)
            family_args_line="$(agents_claude_args)"
            read -r -a family_args <<< "$family_args_line"
            command=(claude --print)
            if [[ "$mode" == "write" ]]; then
                command+=(--dangerously-skip-permissions)
            else
                command+=(--permission-mode plan)
            fi
            command+=(--settings '{"sandbox":{"enabled":false}}')
            command+=("${family_args[@]}")
            if [[ -n "${AGENT_EXEC_EXTRA_ARGS:-}" ]]; then
                command+=("${extra_args[@]}")
            fi
            command+=(-- "$prompt")

            if [[ "${AGENT_EXEC_DRY_RUN:-}" == "1" ]]; then
                printf 'cd '
                printf '%q' "$working_dir"
                printf ' && '
                agents_exec_print_argv "${command[@]}"
                printf ' > '
                printf '%q' "$output_file"
                printf ' 2> '
                printf '%q' "$log_file"
                printf '\n'
                return 0
            fi
            ( cd "$working_dir" && "${command[@]}" > "$output_file" 2> "$log_file" )
            ;;
        *)
            echo "ERROR: unsupported agent family '$AGENT_FAMILY'." >&2
            return 1
            ;;
    esac
}

agents_exec_main "$@"
