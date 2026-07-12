#!/usr/bin/env bash
# Shared delegate profile reader and validator.

DELEGATE_CONFIG_FILE="${DELEGATE_CONFIG_FILE:-$HOME/.claude/config/delegate.conf}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../agents/agents_config.sh"

delegate_config_trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

delegate_config_get() {
    local want_section="$1" key="$2" line stripped section value
    [[ -f "$DELEGATE_CONFIG_FILE" ]] || {
        echo "ERROR: Delegate config not found: $DELEGATE_CONFIG_FILE" >&2
        return 1
    }
    section=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="$(delegate_config_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            continue
        fi
        [[ "$section" == "$want_section" ]] || continue
        if [[ "$stripped" =~ ^${key}=(.*)$ ]]; then
            value="$(delegate_config_trim "${BASH_REMATCH[1]}")"
            printf '%s' "$value"
            return 0
        fi
    done < "$DELEGATE_CONFIG_FILE"
    echo "ERROR: Delegate profile [$want_section] has no '$key' value in $DELEGATE_CONFIG_FILE" >&2
    return 1
}

delegate_config_resolve() {
    local profile="$1"
    DELEGATE_AGENT="$(delegate_config_get "$profile" agent)" || return 1
    DELEGATE_EFFORT="$(delegate_config_get "$profile" effort)" || return 1
    if [[ "$DELEGATE_AGENT" != "codex" ]]; then
        echo "ERROR: Delegate launcher supports agent 'codex', got '$DELEGATE_AGENT' for [$profile]." >&2
        return 1
    fi
    DELEGATE_MODEL="$(agents_config_model "$DELEGATE_AGENT")"
    agents_config_validate_model_for_agent delegate "$DELEGATE_AGENT" "$DELEGATE_MODEL" || return 1
    agents_config_validate_effort_for_agent delegate "$DELEGATE_AGENT" "$DELEGATE_EFFORT" || return 1
}
