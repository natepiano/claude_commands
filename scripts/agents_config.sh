#!/usr/bin/env bash
# Shared per-agent default model/effort reader.
#
# Single source of truth: ~/.claude/config/agents.conf — sectioned [codex]/[claude],
# each with model= / effort= keys. Sibling to clean-fix/agent-models.conf (the
# allowlist); this file picks the one model+effort each agent runs with.
#
# Source this file, then call:
#   agents_config_model <agent>    agents_config_effort <agent>
# Both return an empty string when the section/key/file is absent, so callers fall
# through to the agent's own CLI default.
#
# Consumers: /delegate (codex_implement.sh, codex_review.sh) and clean-fix
# (sourced transitively via clean-fix/agent_config.sh).

AGENTS_CONFIG_FILE="${AGENTS_CONFIG_FILE:-$HOME/.claude/config/agents.conf}"

# _agents_config_get <agent> <key> — print the trimmed value of key=… inside [agent].
_agents_config_get() {
    local want_agent="$1" key="$2" line stripped section value
    [[ -f "$AGENTS_CONFIG_FILE" ]] || return 0
    section=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="${stripped#"${stripped%%[![:space:]]*}"}"
        stripped="${stripped%"${stripped##*[![:space:]]}"}"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            continue
        fi
        [[ "$section" == "$want_agent" ]] || continue
        if [[ "$stripped" =~ ^${key}=(.*)$ ]]; then
            value="${BASH_REMATCH[1]}"
            value="${value#"${value%%[![:space:]]*}"}"
            value="${value%"${value##*[![:space:]]}"}"
            printf '%s' "$value"
            return 0
        fi
    done < "$AGENTS_CONFIG_FILE"
}

agents_config_model() { _agents_config_get "$1" model; }
agents_config_effort() { _agents_config_get "$1" effort; }
