#!/usr/bin/env bash
# Resolves which agent (codex|claude) the ~/.zshrc CLI aliases — merge,
# code — should run, then runs it.
#
# Family, model, and effort assignments live in the global registry,
# ~/.claude/config/models.conf, via scripts/agents/models_config.sh.
#
# Usage:
#   cli_agent.sh                    # no skill invocation -> launch agent's interactive REPL
#   cli_agent.sh <skill> [args...]  # run <skill> [args...] non-interactively via the configured agent
#   cli_agent.sh --status           # print the resolved CLI assignments and exit

set -euo pipefail

CLI_AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$CLI_AGENT_DIR/../agents/models_config.sh"

cli_agent_print_status() {
    agents_resolve_print cli.merge_branch
    agents_resolve_print cli.interactive
}

# cli_agent_run <skill...> — no args launches the agent's interactive REPL;
# args are joined into one non-interactive skill invocation.
cli_agent_run() {
    local task invocation skill workers_home
    local -a agent_args=()

    if [[ $# -eq 0 ]]; then
        task="cli.interactive"
        invocation=""
    else
        skill="$1"
        case "$skill" in
            merge_branch)
                task="cli.$skill"
                ;;
            *)
                echo "ERROR: unknown skill '$skill'. Known skills: merge_branch." >&2
                return 1
                ;;
        esac
        invocation="$*"
    fi

    agents_resolve "$task" || return 1

    case "$AGENT_FAMILY" in
        codex)
            workers_home="$(agents_workers_home)" || return 1
            read -r -a agent_args <<< "$(agents_codex_args)"
            models_agent_environment
            if [[ -z "$invocation" ]]; then
                exec env "CODEX_HOME=$workers_home" codex "${agent_args[@]}"
            fi
            exec env "CODEX_HOME=$workers_home" codex "${agent_args[@]}" "$invocation"
            ;;
        claude)
            source "$CLI_AGENT_DIR/../agents/claude_account.sh"
            claude_launch_environment workers || return 1
            AGENT_MODEL="$(_agents_model_slug claude "$AGENT_MODEL")" || return 1
            read -r -a agent_args <<< "$(agents_claude_args)"
            models_agent_environment
            if [[ -z "$invocation" ]]; then
                exec "$CLI_AGENT_DIR/../lib/py" "$CLI_AGENT_DIR/../lib/nix_env.py" exec "$(command -v claude)" "${agent_args[@]}"
            fi
            exec "$CLI_AGENT_DIR/../lib/py" "$CLI_AGENT_DIR/../lib/nix_env.py" exec "$(command -v claude)" "${agent_args[@]}" -- "/$invocation"
            ;;
    esac
}

case "${1:-}" in
    --status)
        cli_agent_print_status
        ;;
    --set)
        echo "ERROR: --set has been removed; use /default_models to edit CLI assignments." >&2
        exit 1
        ;;
    *)
        cli_agent_run "$@"
        ;;
esac
