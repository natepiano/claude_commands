#!/usr/bin/env bash
# Resolves which agent (codex|claude) the ~/.zshrc CLI aliases — review,
# commit_no, commit_yes, merge, code — should run, then runs it.
#
# Assignment lives in agent-assignment.conf (this directory). Model/effort
# defaults and allowlists live in the global registry,
# ~/.claude/config/agents.conf, via scripts/agents/agents_config.sh.
#
# Usage:
#   cli_agent.sh                    # no skill invocation -> launch agent's interactive REPL
#   cli_agent.sh <skill> [args...]  # run <skill> [args...] non-interactively via the configured agent
#   cli_agent.sh --status           # print resolved agent/model/effort and exit
#   cli_agent.sh --set <agent> [model] [effort]   # update the assignment

set -euo pipefail

CLI_AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_AGENT_CONF_FILE="${CLI_AGENT_CONF_FILE:-$CLI_AGENT_DIR/agent-assignment.conf}"

source "$CLI_AGENT_DIR/../agents/agents_config.sh"

# cli_agent_load <agent_var> <model_var> <effort_var> — read+validate [cli_agent].
cli_agent_load() {
    local agent_var="$1" model_var="$2" effort_var="$3"
    local line stripped section
    local parsed_agent="" parsed_model="" parsed_effort=""

    if [[ ! -f "$CLI_AGENT_CONF_FILE" ]]; then
        echo "ERROR: cli-agent assignment file not found: $CLI_AGENT_CONF_FILE" >&2
        return 1
    fi

    section=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="$(agents_config_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            continue
        fi
        [[ "$section" == "cli_agent" ]] || continue

        if [[ "$stripped" =~ ^agent=(.+)$ ]]; then
            parsed_agent="${BASH_REMATCH[1]}"
        elif [[ "$stripped" =~ ^model=(.*)$ ]]; then
            parsed_model="${BASH_REMATCH[1]}"
        elif [[ "$stripped" =~ ^effort=(.*)$ ]]; then
            parsed_effort="${BASH_REMATCH[1]}"
        fi
    done < "$CLI_AGENT_CONF_FILE"

    agents_config_validate_agent "cli_agent" "$parsed_agent" || return 1
    agents_config_apply_defaults parsed_model parsed_effort "$parsed_agent"
    agents_config_validate_model_for_agent "cli_agent" "$parsed_agent" "$parsed_model" || return 1
    agents_config_validate_effort_for_agent "cli_agent" "$parsed_agent" "$parsed_effort" || return 1

    printf -v "$agent_var" '%s' "$parsed_agent"
    printf -v "$model_var" '%s' "$parsed_model"
    printf -v "$effort_var" '%s' "$parsed_effort"
}

cli_agent_print_status() {
    local agent="" model="" effort=""
    cli_agent_load agent model effort || return 1
    printf 'cli_agent assignment: %s\n' "$CLI_AGENT_CONF_FILE"
    printf 'global agent registry: %s\n' "$AGENTS_CONFIG_FILE"
    printf 'agent=%-6s model=%-12s effort=%s\n' "$agent" "${model:-<default>}" "${effort:-<default>}"
}

# cli_agent_set <agent> [model] [effort] — validate, then rewrite agent-assignment.conf.
cli_agent_set() {
    local new_agent="$1" new_model="${2:-}" new_effort="${3:-}"

    agents_config_validate_agent "cli_agent" "$new_agent" || return 1
    agents_config_validate_model_for_agent "cli_agent" "$new_agent" "$new_model" || return 1
    agents_config_validate_effort_for_agent "cli_agent" "$new_agent" "$new_effort" || return 1

    local tmp_file
    tmp_file="$(mktemp "${CLI_AGENT_CONF_FILE}.XXXXXX")"
    awk -v agent="$new_agent" -v model="$new_model" -v effort="$new_effort" '
        /^\[cli_agent\]/ { in_section = 1; print; next }
        /^\[/ { in_section = 0; print; next }
        in_section && /^agent=/ { print "agent=" agent; next }
        in_section && /^model=/ { print "model=" model; next }
        in_section && /^effort=/ { print "effort=" effort; next }
        { print }
    ' "$CLI_AGENT_CONF_FILE" > "$tmp_file"
    mv "$tmp_file" "$CLI_AGENT_CONF_FILE"

    cli_agent_print_status
}

# cli_agent_run <skill...> — no args launches the agent's interactive REPL;
# args are joined into one non-interactive skill invocation.
cli_agent_run() {
    local agent="" model="" effort=""
    cli_agent_load agent model effort || return 1

    if [[ $# -eq 0 ]]; then
        case "$agent" in
            codex)
                exec codex -m "$model" -c model_reasoning_effort="\"$effort\"" -c service_tier="fast"
                ;;
            claude)
                exec claude --model "$model" --effort "$effort"
                ;;
        esac
    fi

    local invocation="$*"
    case "$agent" in
        codex)
            exec codex -m "$model" -c model_reasoning_effort="\"$effort\"" -c service_tier="fast" "$invocation"
            ;;
        claude)
            exec claude --model "$model" --effort "$effort" -- "/$invocation"
            ;;
    esac
}

case "${1:-}" in
    --status)
        cli_agent_print_status
        ;;
    --set)
        shift
        cli_agent_set "$@"
        ;;
    *)
        cli_agent_run "$@"
        ;;
esac
