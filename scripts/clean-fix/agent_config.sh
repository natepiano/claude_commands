#!/bin/bash
# Shared clean-fix agent/model validation helpers.

AGENT_CONFIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_MODELS_FILE="${AGENT_MODELS_FILE:-$AGENT_CONFIG_DIR/agent-models.conf}"

cf_trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

cf_validate_bool() {
    local section="$1"
    local key="$2"
    local value="$3"
    case "$value" in
        true|false) return 0 ;;
        *)
            echo "ERROR: [$section] $key must be true or false, got: $value" >&2
            return 1
            ;;
    esac
}

cf_validate_agent() {
    local section="$1"
    local agent="$2"
    case "$agent" in
        claude|codex) return 0 ;;
        "")
            echo "ERROR: [$section] agent must be set to claude or codex" >&2
            return 1
            ;;
        *)
            echo "ERROR: [$section] agent must be claude or codex, got: $agent" >&2
            return 1
            ;;
    esac
}

cf_allowed_models_for_agent() {
    local agent="$1"
    local current_section=""
    local line stripped

    if [[ ! -f "$AGENT_MODELS_FILE" ]]; then
        echo "ERROR: model allowlist not found: $AGENT_MODELS_FILE" >&2
        return 1
    fi

    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="$(cf_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            current_section="${BASH_REMATCH[1]}"
            continue
        fi
        if [[ "$current_section" == "$agent" ]]; then
            echo "$stripped"
        fi
    done < "$AGENT_MODELS_FILE"
}

cf_allowed_models_inline() {
    local agent="$1"
    local first=1
    local model
    while IFS= read -r model; do
        if [[ "$first" -eq 1 ]]; then
            printf '%s' "$model"
            first=0
        else
            printf ', %s' "$model"
        fi
    done < <(cf_allowed_models_for_agent "$agent")
}

cf_model_allowed_for_agent() {
    local agent="$1"
    local requested_model="$2"
    local allowed_model
    while IFS= read -r allowed_model; do
        if [[ "$allowed_model" == "$requested_model" ]]; then
            return 0
        fi
    done < <(cf_allowed_models_for_agent "$agent")
    return 1
}

cf_validate_model_for_agent() {
    local section="$1"
    local agent="$2"
    local model="$3"
    local allowed

    [[ -z "$model" ]] && return 0
    cf_validate_agent "$section" "$agent" || return 1
    if cf_model_allowed_for_agent "$agent" "$model"; then
        return 0
    fi

    allowed="$(cf_allowed_models_inline "$agent")"
    echo "ERROR: [$section] model '$model' is not allowed for agent '$agent'." >&2
    echo "       Allowed $agent models in $AGENT_MODELS_FILE: $allowed" >&2
    return 1
}
