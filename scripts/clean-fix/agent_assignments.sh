#!/usr/bin/env bash
# Clean-fix stage assignment reader.
#
# Clean-fix owns which stage uses which agent. The global agent registry
# (~/.claude/config/agents.conf) owns model/effort defaults and allowlists.

CLEAN_FIX_AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLEAN_FIX_AGENT_ASSIGNMENTS_FILE="${CLEAN_FIX_AGENT_ASSIGNMENTS_FILE:-$CLEAN_FIX_AGENT_DIR/agent-assignments.conf}"

source "$CLEAN_FIX_AGENT_DIR/../agents_config.sh"

cf_trim() { agents_config_trim "$1"; }

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

cf_validate_agent() { agents_config_validate_agent "$@"; }
cf_validate_model_for_agent() { agents_config_validate_model_for_agent "$@"; }
cf_validate_effort_for_agent() { agents_config_validate_effort_for_agent "$@"; }
cf_apply_agent_defaults() { agents_config_apply_defaults "$@"; }
cf_allowed_models_for_agent() { agents_config_allowed_models "$@"; }
cf_allowed_models_inline() { agents_config_allowed_models_inline "$@"; }
cf_model_allowed_for_agent() {
    local agent="$1" model="$2" allowed
    while IFS= read -r allowed; do
        [[ "$allowed" == "$model" ]] && return 0
    done < <(agents_config_allowed_models "$agent")
    return 1
}

# cf_load_stage_assignment <section> <enabled_var> <agent_var> <model_var> <effort_var>
cf_load_stage_assignment() {
    local want_section="$1"
    local enabled_var="$2"
    local agent_var="$3"
    local model_var="$4"
    local effort_var="$5"
    local line stripped section
    local parsed_enabled="" parsed_agent="" parsed_model="" parsed_effort=""

    if [[ ! -f "$CLEAN_FIX_AGENT_ASSIGNMENTS_FILE" ]]; then
        echo "ERROR: clean-fix agent assignment file not found: $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE" >&2
        return 1
    fi

    section=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line%%#*}"
        stripped="$(cf_trim "$stripped")"
        [[ -z "$stripped" ]] && continue
        if [[ "$stripped" =~ ^\[(.+)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            continue
        fi
        [[ "$section" == "$want_section" ]] || continue

        if [[ "$stripped" =~ ^mode= ]]; then
            echo "ERROR: [$want_section] mode is no longer supported; use enabled=true|false and agent=<name>" >&2
            return 1
        elif [[ "$stripped" =~ ^enabled=(.+)$ ]]; then
            parsed_enabled="${BASH_REMATCH[1]}"
        elif [[ "$stripped" =~ ^agent=(.+)$ ]]; then
            parsed_agent="${BASH_REMATCH[1]}"
        elif [[ "$stripped" =~ ^model=(.*)$ ]]; then
            parsed_model="${BASH_REMATCH[1]}"
        elif [[ "$stripped" =~ ^effort=(.*)$ ]]; then
            parsed_effort="${BASH_REMATCH[1]}"
        fi
    done < "$CLEAN_FIX_AGENT_ASSIGNMENTS_FILE"

    if [[ -z "$parsed_enabled" ]]; then
        echo "ERROR: [$want_section] enabled must be set to true or false in $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE" >&2
        return 1
    fi
    cf_validate_bool "$want_section" enabled "$parsed_enabled" || return 1
    cf_validate_agent "$want_section" "$parsed_agent" || return 1
    cf_apply_agent_defaults parsed_model parsed_effort "$parsed_agent"
    cf_validate_model_for_agent "$want_section" "$parsed_agent" "$parsed_model" || return 1
    cf_validate_effort_for_agent "$want_section" "$parsed_agent" "$parsed_effort" || return 1

    printf -v "$enabled_var" '%s' "$parsed_enabled"
    printf -v "$agent_var" '%s' "$parsed_agent"
    printf -v "$model_var" '%s' "$parsed_model"
    printf -v "$effort_var" '%s' "$parsed_effort"
}

cf_print_stage_assignment() {
    local section="$1"
    local enabled="" agent="" model="" effort=""
    cf_load_stage_assignment "$section" enabled agent model effort || return 1
    printf '%-18s enabled=%-5s agent=%-6s model=%-12s effort=%s\n' \
        "$section" "$enabled" "$agent" "${model:-<default>}" "${effort:-<default>}"
}

cf_print_agent_assignments() {
    echo "clean-fix assignments: $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE"
    echo "global agent registry: $AGENTS_CONFIG_FILE"
    cf_print_stage_assignment style_eval
    cf_print_stage_assignment style_eval_review
    cf_print_stage_assignment style_fix
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    cf_print_agent_assignments
fi
