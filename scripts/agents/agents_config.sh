#!/usr/bin/env bash
# Shared agent registry/default reader.
#
# Single source of truth: ~/.claude/config/agents.conf. Command-specific areas
# choose their own stage assignments, then use this file for defaults and
# validation against the global model/effort allowlists.

AGENTS_CONFIG_FILE="${AGENTS_CONFIG_FILE:-$HOME/.claude/config/agents.conf}"
AGENTS_CONFIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_CONFIG_FILE="${CODEX_CONFIG_FILE:-$HOME/.codex/config.toml}"
CODEX_MODELS_CACHE_FILE="${CODEX_MODELS_CACHE_FILE:-$HOME/.codex/models_cache.json}"
CODEX_CATALOG_SYNC_STATE_FILE="${CODEX_CATALOG_SYNC_STATE_FILE:-$HOME/.local/state/codex-agent-catalog-sync/last_success}"

# Keep the materialized registry current between periodic launchd runs.
if [[ -f "$AGENTS_CONFIG_FILE" \
    && -x "$AGENTS_CONFIG_DIR/sync_codex_catalog.sh" \
    && ( ! -f "$CODEX_CATALOG_SYNC_STATE_FILE" \
        || "$CODEX_CONFIG_FILE" -nt "$CODEX_CATALOG_SYNC_STATE_FILE" \
        || "$CODEX_MODELS_CACHE_FILE" -nt "$CODEX_CATALOG_SYNC_STATE_FILE" ) ]]; then
    if ! "$AGENTS_CONFIG_DIR/sync_codex_catalog.sh" >/dev/null; then
        echo "WARNING: Codex catalog sync failed; using $AGENTS_CONFIG_FILE as-is." >&2
    fi
fi

agents_config_trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

# _agents_config_get <section> <key> — print the trimmed value of key=... inside [section].
_agents_config_get() {
    local want_section="$1" key="$2" line stripped section value
    [[ -f "$AGENTS_CONFIG_FILE" ]] || return 0
    section=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="$(agents_config_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            continue
        fi
        [[ "$section" == "$want_section" ]] || continue
        if [[ "$stripped" =~ ^${key}=(.*)$ ]]; then
            value="${BASH_REMATCH[1]}"
            value="$(agents_config_trim "$value")"
            printf '%s' "$value"
            return 0
        fi
    done < "$AGENTS_CONFIG_FILE"
}

_agents_config_has_section() {
    local want_section="$1" line stripped section
    [[ -f "$AGENTS_CONFIG_FILE" ]] || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="$(agents_config_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            [[ "$section" == "$want_section" ]] && return 0
        fi
    done < "$AGENTS_CONFIG_FILE"
    return 1
}

_agents_config_section_values() {
    local want_section="$1" line stripped section
    [[ -f "$AGENTS_CONFIG_FILE" ]] || return 0
    section=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="$(agents_config_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            continue
        fi
        [[ "$section" == "$want_section" ]] || continue
        printf '%s\n' "$stripped"
    done < "$AGENTS_CONFIG_FILE"
}

_agents_config_value_allowed() {
    local section="$1" value="$2" allowed
    while IFS= read -r allowed; do
        [[ "$allowed" == "$value" ]] && return 0
    done < <(_agents_config_section_values "$section")
    return 1
}

_agents_config_values_inline() {
    local section="$1" first=1 value
    while IFS= read -r value; do
        if [[ "$first" -eq 1 ]]; then
            printf '%s' "$value"
            first=0
        else
            printf ', %s' "$value"
        fi
    done < <(_agents_config_section_values "$section")
}

agents_config_model() { _agents_config_get "$1" model; }
agents_config_effort() { _agents_config_get "$1" effort; }
agents_config_allowed_models() { _agents_config_section_values "$1.models"; }
agents_config_allowed_efforts() { _agents_config_section_values "$1.efforts"; }
agents_config_allowed_models_inline() { _agents_config_values_inline "$1.models"; }
agents_config_allowed_efforts_inline() { _agents_config_values_inline "$1.efforts"; }

agents_config_validate_agent() {
    local context="$1" agent="$2"
    if [[ -z "$agent" ]]; then
        echo "ERROR: [$context] agent must be set" >&2
        return 1
    fi
    if _agents_config_has_section "$agent"; then
        return 0
    fi
    echo "ERROR: [$context] unknown agent '$agent' in $AGENTS_CONFIG_FILE" >&2
    return 1
}

agents_config_validate_model_for_agent() {
    local context="$1" agent="$2" model="$3" allowed
    [[ -z "$model" ]] && return 0
    agents_config_validate_agent "$context" "$agent" || return 1
    if _agents_config_value_allowed "$agent.models" "$model"; then
        return 0
    fi

    allowed="$(agents_config_allowed_models_inline "$agent")"
    echo "ERROR: [$context] model '$model' is not allowed for agent '$agent'." >&2
    echo "       Allowed models in $AGENTS_CONFIG_FILE [$agent.models]: $allowed" >&2
    return 1
}

agents_config_validate_effort_for_agent() {
    local context="$1" agent="$2" effort="$3" allowed
    [[ -z "$effort" ]] && return 0
    agents_config_validate_agent "$context" "$agent" || return 1
    if _agents_config_value_allowed "$agent.efforts" "$effort"; then
        return 0
    fi

    allowed="$(agents_config_allowed_efforts_inline "$agent")"
    echo "ERROR: [$context] effort '$effort' is not allowed for agent '$agent'." >&2
    echo "       Allowed effort levels in $AGENTS_CONFIG_FILE [$agent.efforts]: $allowed" >&2
    return 1
}

agents_config_apply_defaults() {
    local model_var="$1" effort_var="$2" agent="$3"
    if [[ -z "${!model_var}" ]]; then
        printf -v "$model_var" '%s' "$(agents_config_model "$agent")"
    fi
    if [[ -z "${!effort_var}" ]]; then
        printf -v "$effort_var" '%s' "$(agents_config_effort "$agent")"
    fi
}
