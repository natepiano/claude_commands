#!/usr/bin/env bash
# Resolves which agent (codex|claude) the ~/.zshrc CLI aliases — review,
# commit_no, commit_yes, merge, code — should run, then runs it.
#
# Family, model, and effort assignments live in the global registry,
# ~/.claude/config/agents.conf, via scripts/agents/agents_config.sh.
#
# Usage:
#   cli_agent.sh                    # no skill invocation -> launch agent's interactive REPL
#   cli_agent.sh <skill> [args...]  # run <skill> [args...] non-interactively via the configured agent
#   cli_agent.sh --status           # print the resolved CLI assignments and exit

set -euo pipefail

CLI_AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$CLI_AGENT_DIR/../agents/agents_config.sh"

cli_agent_print_status() {
    agents_resolve_print cli.style_fix_review
    agents_resolve_print cli.commit_prep
    agents_resolve_print cli.merge_branch
    agents_resolve_print cli.interactive
}

# cli_agent_run <skill...> — no args launches the agent's interactive REPL;
# args are joined into one non-interactive skill invocation.
cli_agent_run() {
    local task invocation skill
    local -a agent_args=()

    if [[ $# -eq 0 ]]; then
        task="cli.interactive"
        invocation=""
    else
        skill="$1"
        case "$skill" in
            style_fix_review|commit_prep|merge_branch)
                task="cli.$skill"
                ;;
            *)
                echo "ERROR: unknown skill '$skill'. Known skills: style_fix_review, commit_prep, merge_branch." >&2
                return 1
                ;;
        esac
        invocation="$*"
    fi

    agents_resolve "$task" || return 1

    case "$AGENT_FAMILY" in
        codex)
            read -r -a agent_args <<< "$(agents_codex_args)"
            if [[ -z "$invocation" ]]; then
                exec codex "${agent_args[@]}" -c service_tier="fast"
            fi
            exec codex "${agent_args[@]}" -c service_tier="fast" "$invocation"
            ;;
        claude)
            read -r -a agent_args <<< "$(agents_claude_args)"
            if [[ -z "$invocation" ]]; then
                exec claude "${agent_args[@]}"
            fi
            exec claude "${agent_args[@]}" -- "/$invocation"
            ;;
    esac
}

case "${1:-}" in
    --status)
        cli_agent_print_status
        ;;
    --set)
        echo "ERROR: --set has been removed; use /agent to edit CLI assignments." >&2
        exit 1
        ;;
    *)
        cli_agent_run "$@"
        ;;
esac
